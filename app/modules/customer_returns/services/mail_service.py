"""Customer-Returns approval e-mail (threaded, HTML + plain, magic-link buttons).

Faithful to the legacy shared/email_notifier.py RTV flow, adapted to the live
async backend:
  * deterministic thread id ``RTV-<cr_id>@candorfoods.in`` — In-Reply-To +
    References on EVERY mail, the approval request included, each of which has its
    own fresh Message-ID (_send_sync). No DB column (pure function of the id), so
    every copy lands in one conversation per CR. The thread id is never sent as a
    Message-ID: the approval request is also the Re-send, and IMS sent its own
    first mail under that same id — Gmail drops an incoming message whose
    Message-ID is already in the mailbox, so a re-sent copy (with its fresh links)
    would never arrive while the send reported success.
  * only a DECIDER's copy of the "created" mail carries the Approve / Reject /
    Hold buttons (signed magic links -> the public /email-action endpoint, which
    shows a confirmation page; only its Confirm POST decides) and a link to the
    in-app review; everyone else gets a button-less copy in the same thread.
  * an approver is the CR's Business Head PLUS the standing deputies configured as
    ``deputy_approver`` in cr_email_routing (resolve_approvers). A return must close
    the day it is raised, and one named BU Head is a single point of failure. The
    deciders are those approvers minus the person who raised the return
    (eligible_approvers): approval_service never lets anyone decide their own
    return, so they get the button-less copy, not buttons that would only refuse
    them. Each decider gets their OWN copy carrying a token minted for their own
    address, so the audit records who actually decided rather than crediting the
    BU Head for a deputy's click. The same rule is who approval_service lets
    decide, on every channel. The decision itself is idempotent, so two approvers
    acting at once is safe — the first wins, the second is told it is already
    actioned.
  * recipient routing comes from the DB (fetch_routing): BH/sales POC by role
    (auth_user), warehouse-owner / constant CC / notify-to from cr_email_routing.
  * best-effort: a missing SMTP config or a send failure is logged and never fails
    the API request (mirrors production/mail_service) — but it is REPORTED: every
    send returns what happened, and the notifiers return a ``mail`` outcome
    ({sent, to, cc_count, reason?}) the send-for-approval response passes on, so a
    mail that did not go out is never shown as sent.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from urllib.parse import quote

from app.config import Settings
from app.core.mail_identity import Module, SubjectPolicy, stamp
from app.core.sites import canonical_site, same_site
from app.modules.customer_returns.approval_token import make_action_token

logger = logging.getLogger(__name__)

ACTION_META = [
    ("approve", "Approve", "#27ae60"),
    ("reject", "Reject", "#c0392b"),
    ("hold", "Hold", "#e67e22"),
]


# ── threading ────────────────────────────────────────────────────────────────
def _thread_key(cr_id: str) -> str:
    return f"RTV-{cr_id}@candorfoods.in"


# ── recipient routing (same source as GET /email-routing) ─────────────────────
async def fetch_routing(conn) -> dict:
    """business_head / sales_poc by role (auth_user); warehouse_cc / constant_cc /
    notify_to from cr_email_routing. Degrades gracefully if the config table is
    missing (migration not yet applied)."""
    people = await conn.fetch(
        """
        SELECT DISTINCT u.full_name AS name, COALESCE(u.email, '') AS email, r.role_name
        FROM auth_user u
        JOIN auth_role r ON r.role_name IN ('business_head', 'sales')
        WHERE u.is_active
          AND (u.role_id = r.role_id
               OR EXISTS (SELECT 1 FROM auth_user_role ur
                          WHERE ur.user_id = u.user_id AND ur.role_id = r.role_id))
        ORDER BY u.full_name
        """
    )
    try:
        cfg = await conn.fetch(
            "SELECT kind, match_key, match_type, email FROM cr_email_routing "
            "WHERE active AND kind IN ('warehouse_cc', 'constant_cc', 'notify_to', "
            "'deputy_approver') "
            "ORDER BY kind, sort_order, match_key"
        )
    except Exception:
        # Still degrade, but say so: without these rows the deputies cannot decide
        # (approval_service) and notify_to / every CC drop off the mail.
        logger.warning("[cr-mail] cr_email_routing unreadable — no deputies, notify_to or CC",
                       exc_info=True)
        cfg = []
    return {
        "business_head": [{"name": p["name"], "email": p["email"]} for p in people if p["role_name"] == "business_head"],
        "sales_poc": [{"name": p["name"], "email": p["email"]} for p in people if p["role_name"] == "sales"],
        "warehouse_cc": [{"match_key": r["match_key"], "match_type": r["match_type"], "email": r["email"]} for r in cfg if r["kind"] == "warehouse_cc"],
        "constant_cc": [r["email"] for r in cfg if r["kind"] == "constant_cc"],
        "notify_to": next((r["email"] for r in cfg if r["kind"] == "notify_to"), None),
        # Standing deputies. Config rows rather than a role, because these two act
        # for ANY CR's BU Head — holding the `business_head` role instead would put
        # them in the create form's BU-Head dropdown as if a return could be
        # assigned to them, which is not what a deputy is. `name` is their
        # auth_user.full_name; the WhatsApp leg resolves a phone from it.
        "deputy_approver": [{"name": r["match_key"], "email": r["email"]}
                            for r in cfg if r["kind"] == "deputy_approver"],
    }


def _ci_email(rows: list[dict], key) -> str | None:
    if not key:
        return None
    k = str(key).strip().lower()
    for r in rows:
        if (r.get("name") or "").strip().lower() == k:
            return r.get("email") or None
    return None


def lookup_business_head_email(routing: dict, bh) -> str | None:
    return _ci_email(routing.get("business_head", []), bh)


def _warehouse_cc_email(routing: dict, factory_unit) -> str | None:
    """The warehouse owner's CC for the CR's factory unit, by its site (app.core.sites):
    a 'substring' row matches when its key is in the stored unit or in that site's
    canonical name ('savla' finds 'D-39', which is Savla D-39); a 'code' row matches
    any spelling of its own site ('a185' finds 'Warehouse A185' and 'A-185')."""
    if not factory_unit:
        return None
    raw = str(factory_unit).strip()
    names = (raw.lower(), canonical_site(raw).lower())
    for r in routing.get("warehouse_cc", []):
        key = (r.get("match_key") or "").strip().lower()
        if not key:
            continue
        if r.get("match_type") == "substring" and any(key in n for n in names):
            return r["email"]
        if r.get("match_type") == "code" and same_site(key, raw):
            return r["email"]
    return None


def resolve_approvers(routing: dict, business_head) -> list[dict]:
    """[{name, email}] holding the Approve / Reject / Hold buttons — and the only
    people approval_service lets decide, on every channel: the CR's Business Head
    when their name resolves to an e-mail (lookup_business_head_email), then each
    standing deputy, deduped by e-mail (trimmed, case-insensitive). BH first."""
    bh_email = (lookup_business_head_email(routing, business_head) or "").strip()
    approvers: list[dict] = []
    if bh_email:
        approvers.append({"name": str(business_head or "").strip() or bh_email, "email": bh_email})
    for d in routing.get("deputy_approver", []):
        email = (d.get("email") or "").strip()
        if email and email.lower() not in {a["email"].lower() for a in approvers}:
            approvers.append({"name": (d.get("name") or "").strip() or email, "email": email})
    return approvers


def eligible_approvers(approvers: list[dict], created_by) -> list[dict]:
    """The approvers who can decide THIS return: ``approvers`` minus the person who
    raised it (created_by; e-mails compared trimmed, case-insensitively), whom
    approval_service refuses (own_return) on every channel. Only they get the
    Approve / Reject / Hold buttons, by mail and WhatsApp, and only they count as an
    approver reached when the request is sent."""
    raised_by = str(created_by or "").strip().lower()
    return [a for a in approvers if not raised_by or a["email"].strip().lower() != raised_by]


def resolve_recipients(routing: dict, header: dict) -> dict:
    """{approver, approvers, deciders, to, cc} — port of the FE
    _approvalMatrix.resolveRecipients.

    The BH (if mapped) and the standing deputies are the approvers; ``deciders`` are
    the ones who can decide this return — all but whoever raised it
    (eligible_approvers). The deciders lead TO alongside notify_to. CC = sales POC
    (mapped + manual) + constants + creator + warehouse owner, deduped, with the TO
    addresses removed — so a creator who is also an approver is copied, not asked.

    ``approver`` stays the PRIMARY alone — it is what the UI labels "Business Head"
    and what the response payload has always meant (even when they raised it).
    ``approvers`` is the full list the rule names, primary first.
    """
    approvers = resolve_approvers(routing, header.get("business_head"))
    deciders = eligible_approvers(approvers, header.get("created_by"))
    has_bh = bool((lookup_business_head_email(routing, header.get("business_head")) or "").strip())
    approver = approvers[0] if has_bh else None   # resolve_approvers puts the BH first

    to = [e for e in [*(a["email"] for a in deciders), routing.get("notify_to")] if e]
    to_lower = {a.strip().lower() for a in to}

    candidates: list[str] = []
    poc = _ci_email(routing.get("sales_poc", []), header.get("sales_poc"))
    if poc:
        candidates.append(poc)
    if header.get("sales_poc_email") and str(header["sales_poc_email"]).strip():
        candidates.append(str(header["sales_poc_email"]).strip())
    candidates.extend(routing.get("constant_cc", []))
    if header.get("created_by"):
        candidates.append(str(header["created_by"]))
    wh = _warehouse_cc_email(routing, header.get("factory_unit"))
    if wh:
        candidates.append(wh)

    seen: set[str] = set()
    cc: list[str] = []
    for addr in candidates:
        n = (addr or "").strip().lower()
        if not n or n in seen or n in to_lower:
            continue
        seen.add(n)
        cc.append(addr.strip())
    return {"approver": approver, "approvers": approvers, "deciders": deciders, "to": to, "cc": cc}


# ── SMTP send (best-effort, off the event loop) ───────────────────────────────
def _send_sync(subject: str, plain: str, html: str, to_addrs: list[str], cc_addrs: list[str],
               in_reply_to: str | None) -> dict:
    """Send one mail. Never raises; returns what happened — {"sent": True} or
    {"sent": False, "reason": "smtp_not_configured" | "no_recipients" | "smtp_error"}
    — so a skipped or failed mail is reported, not mistaken for a sent one.

    Every mail gets its own fresh Message-ID and replies into ``in_reply_to`` (the
    CR's thread id): no two sends ever share a Message-ID, so a mail provider never
    drops one as a duplicate of the last."""
    try:
        s = Settings()
        host = (s.SMTP_HOST or "").strip()
        sender = (s.SMTP_EMAIL or "").strip()
        if not host:
            logger.info("[cr-mail] SMTP_HOST not configured — skipping (subject=%r)", subject)
            return {"sent": False, "reason": "smtp_not_configured"}
        # Drop None / blank recipients (e.g. an unseeded notify_to before the routing
        # migration is applied) — a best-effort mail must never raise. Promote CC to To
        # if no explicit To survives, and skip if nothing is left.
        to_addrs = [a.strip() for a in (to_addrs or []) if a and str(a).strip()]
        cc_addrs = [a.strip() for a in (cc_addrs or []) if a and str(a).strip()]
        if not to_addrs:
            to_addrs, cc_addrs = cc_addrs, []
        if not to_addrs:
            logger.info("[cr-mail] no recipients resolvable — skipping (subject=%r)", subject)
            return {"sent": False, "reason": "no_recipients"}
        recipients = list(dict.fromkeys([*to_addrs, *cc_addrs]))
        # Building the message is inside the try too: a header value the email
        # package refuses (a CR/LF in an address) is a failed send, not a 500.
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(to_addrs)
        if cc_addrs:
            msg["Cc"] = ", ".join(cc_addrs)
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain="candorfoods.in")
        if in_reply_to:
            msg["In-Reply-To"] = f"<{in_reply_to}>"
            msg["References"] = f"<{in_reply_to}>"
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")

        # ANCHOR: customer returns thread one conversation per CR, and that depends on
        # every mail sharing the exact subject. Only the constant module glyph is added.
        stamp(msg, module=Module.RETURNS, policy=SubjectPolicy.ANCHOR,
              entity_type="CustomerReturn", sender=sender)

        with smtplib.SMTP(host, s.SMTP_PORT, timeout=20) as srv:
            srv.starttls(context=ssl.create_default_context())
            if sender:
                srv.login(sender, (s.SMTP_APP_PASSWORD or "").strip())
            srv.send_message(msg, from_addr=sender, to_addrs=recipients)
        logger.info("[cr-mail] sent subject=%r to=%s cc=%s", subject, to_addrs, cc_addrs)
        return {"sent": True}
    except Exception:
        logger.exception("[cr-mail] send failed subject=%r to=%s", subject, to_addrs)
        return {"sent": False, "reason": "smtp_error"}


async def _send(subject, plain, html, to_addrs, cc_addrs, *, in_reply_to=None) -> dict:
    return await asyncio.to_thread(_send_sync, subject, plain, html, to_addrs, cc_addrs, in_reply_to)


def _mail_outcome(rec: dict, sends: list[tuple[list, dict]], approvers_mailed: int) -> dict:
    """The ``mail`` part of a notifier's result. ``sends`` is [(addresses, result)] for
    every mail attempted. sent = every one went out; otherwise ``reason`` is the first
    failure's and ``failed`` lists the addresses that were not mailed."""
    failed = [(addrs, res) for addrs, res in sends if not res.get("sent")]
    out = {"sent": bool(sends) and not failed, "to": rec["to"], "cc_count": len(rec["cc"]),
           "approvers_mailed": approvers_mailed}
    if failed or not sends:
        out["reason"] = (failed[0][1].get("reason") if failed else None) or "error"
        out["failed"] = list(dict.fromkeys(a for addrs, _ in failed for a in addrs if a))
    return out


# ── body builders ─────────────────────────────────────────────────────────────
def _action_url(base: str, company: str, cr_id: str, action: str, approver_email: str) -> str:
    token = make_action_token(company, cr_id, action, approver_email)
    return f"{base.rstrip('/')}/api/v1/customer-returns/email-action?token={token}"


def _review_url(web: str, company: str, cr_id: str) -> str | None:
    """The in-app review page (sign-in needed), where an approver can also decide —
    the way in for a Business Head or deputy who isn't an ERP admin."""
    web = (web or "").strip().rstrip("/")
    if not web:
        return None
    return f"{web}/modules/customer-returns/{quote(cr_id, safe='')}/approve?company={quote(company, safe='')}"


def _info_rows(detail: dict) -> list[tuple[str, str]]:
    return [
        ("Customer", detail.get("customer") or "—"),
        ("Factory Unit", detail.get("factory_unit") or "—"),
        ("Invoice No", detail.get("invoice_number") or "—"),
        ("Sales POC", detail.get("sales_poc") or "—"),
        ("Business Head", detail.get("business_head") or "—"),
        ("Created By", detail.get("created_by") or "—"),
    ]


def _lines_summary(detail: dict) -> tuple[int, float, float]:
    lines = detail.get("lines") or []
    n = len(lines)
    qty = sum(float(l.get("qty") or 0) for l in lines)
    val = sum(float(l.get("value") or 0) for l in lines)
    return n, qty, val


def _html_body(detail: dict, *, buttons: list[tuple[str, str, str]] | None, status_note: str | None,
               review_url: str | None = None) -> str:
    cr_id = escape(str(detail.get("rtv_id") or ""))
    status = escape(str(detail.get("status") or ""))
    info = "".join(
        f'<tr><td style="padding:3px 12px 3px 0;color:#555;">{escape(k)}</td>'
        f'<td style="padding:3px 0;font-weight:600;">{escape(str(v))}</td></tr>'
        for k, v in _info_rows(detail)
    )
    n, qty, val = _lines_summary(detail)
    line_rows = "".join(
        f'<tr>'
        f'<td style="padding:4px 8px;border-top:1px solid #eee;">{escape(str(l.get("item_description") or ""))}</td>'
        f'<td style="padding:4px 8px;border-top:1px solid #eee;text-align:right;">{escape(str(l.get("qty") or 0))}</td>'
        f'<td style="padding:4px 8px;border-top:1px solid #eee;text-align:right;">{escape(str(l.get("rate") or 0))}</td>'
        f'<td style="padding:4px 8px;border-top:1px solid #eee;text-align:right;">{escape(str(l.get("value") or 0))}</td>'
        f'</tr>'
        for l in (detail.get("lines") or [])
    )
    note_html = f'<p style="font-size:14px;color:#333;">{escape(status_note)}</p>' if status_note else ""
    buttons_html = ""
    if buttons:
        cells = "".join(
            f'<a href="{url}" style="display:inline-block;padding:10px 24px;margin:0 6px;'
            f'background:{color};color:#fff;text-decoration:none;font-weight:bold;'
            f'border-radius:6px;font-size:14px;">{escape(label)}</a>'
            for label, url, color in buttons
        )
        buttons_html = (
            f'<div style="text-align:center;margin:14px 0 22px;">{cells}</div>'
            f'<p style="text-align:center;font-size:11px;color:#999;margin:-14px 0 18px;">'
            f'Opens a page to confirm your decision — no login needed. Link expires in 14 days.</p>'
        )
        if review_url:
            buttons_html += (
                f'<p style="text-align:center;font-size:12px;color:#555;margin:-8px 0 18px;">'
                f'Or <a href="{escape(review_url)}" style="color:#555;">review it in the ERP</a>'
                f' (sign-in needed).</p>'
            )
    return (
        f'<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;color:#222;">'
        f'<h2 style="margin:0 0 4px;">Customer Return {cr_id}</h2>'
        f'<p style="margin:0 0 14px;color:#666;">Status: <b>{status}</b></p>'
        f'{note_html}'
        f'{buttons_html}'
        f'<table style="border-collapse:collapse;font-size:14px;margin-bottom:16px;">{info}</table>'
        f'<h3 style="font-size:14px;margin:0 0 6px;">Line Items ({n}) — Total Qty {qty:g}, Total Value {val:g}</h3>'
        f'<table style="border-collapse:collapse;width:100%;font-size:13px;">'
        f'<tr style="text-align:left;color:#555;"><th style="padding:4px 8px;">Item</th>'
        f'<th style="padding:4px 8px;text-align:right;">Qty</th>'
        f'<th style="padding:4px 8px;text-align:right;">Rate</th>'
        f'<th style="padding:4px 8px;text-align:right;">Value</th></tr>{line_rows}</table>'
        f'</div>'
    )


def _plain_body(detail: dict, *, buttons: list[tuple[str, str, str]] | None, status_note: str | None,
                review_url: str | None = None) -> str:
    lines = [f"Customer Return {detail.get('rtv_id')}", f"Status: {detail.get('status')}", ""]
    if status_note:
        lines += [status_note, ""]
    for k, v in _info_rows(detail):
        lines.append(f"{k}: {v}")
    n, qty, val = _lines_summary(detail)
    lines += ["", f"Line Items ({n}) — Total Qty {qty:g}, Total Value {val:g}"]
    for l in (detail.get("lines") or []):
        lines.append(f"  - {l.get('item_description')}  qty={l.get('qty')} rate={l.get('rate')} value={l.get('value')}")
    if buttons:
        lines += ["", "Actions (each opens a page to confirm; no login; expires in 14 days):"]
        lines += [f"  {label}: {url}" for label, url, _ in buttons]
        if review_url:
            lines += ["", f"Or review it in the ERP (sign-in needed): {review_url}"]
    return "\n".join(lines)


def _approver_note(approvers: list[dict], me: int, *, is_bh: bool | None = None,
                   raised_by: str | None = None) -> str:
    """The line above the buttons on an approver's copy. ``approvers`` are the
    deciders — every copy with buttons — and ``me`` is this copy's index.

    It has to say the other approvers exist, or the deputies read their copy as a
    mistake and wait for the BU Head — which is the delay this whole change is
    meant to remove. It also has to say the first decision wins, so nobody holds
    off in case someone else is mid-click.

    ``is_bh`` says whether this copy's approver is the CR's Business Head. With no
    mapped BH — or a BH who raised the return — the first decider is a deputy, so
    position alone can't tell; it defaults to the old reading (first = BH) for
    callers that don't pass it. ``raised_by`` names an approver who raised the
    return and so is not among the deciders: said here, or a deputy waits for a
    Business Head who is not allowed to decide it.
    """
    others = [a["name"] for i, a in enumerate(approvers) if i != me]
    raised = f" {raised_by} raised this return, so cannot decide it." if raised_by else ""
    if not others:
        return "Please Approve / Reject / Hold this customer return using the buttons below." + raised
    who = others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]
    bh = (me == 0) if is_bh is None else is_bh
    role = "You are the assigned Business Head." if bh else "You are a standing deputy approver."
    return (f"Please Approve / Reject / Hold this customer return today. {role} "
            f"{who} can also action it — whoever decides first is the decision.{raised}")


def _subject(cr_id: str, *, reply: bool) -> str:
    base = f"Customer Returns Created: {cr_id}"
    return f"Re: {base}" if reply else base


# ── public notifiers ──────────────────────────────────────────────────────────
async def notify_cr_created(conn, detail: dict) -> dict:
    """Send the approval mail: one copy per decider (an approver who did not raise
    the return) WITH Approve/Reject/Hold buttons, plus a button-less copy to
    notify_to + CC — the creator among them, even when they are an approver.
    Returns the resolved recipients for the caller to surface, plus ``mail`` — what
    actually went out (_mail_outcome; ``approvers_mailed`` counts the button copies
    delivered). Best-effort: once the recipients are resolved nothing here raises;
    a failure is logged and reported in ``mail``.

    Sending again is the Re-send: every copy is a reply into the CR's thread with a
    fresh Message-ID (_send_sync), so a repeat is never dropped as a duplicate."""
    routing = await fetch_routing(conn)
    rec = resolve_recipients(routing, detail)
    cr_id = str(detail.get("rtv_id"))
    company = str(detail.get("company") or "")
    thread = _thread_key(cr_id)
    subject = _subject(cr_id, reply=False)
    sends: list[tuple[list, dict]] = []
    approvers_mailed = 0
    deciders = rec["deciders"]
    bh_email = ((rec["approver"] or {}).get("email") or "").strip().lower()
    # An approver who raised the return (at most one: the creator) is not a decider.
    raised_by = next((a["name"] for a in rec["approvers"] if a not in deciders), None)

    try:
        s = Settings()
        base = (s.PUBLIC_BACKEND_URL or "").strip()
        review = _review_url(s.WEB_APP_URL, company, cr_id)
        if deciders:
            for i, a in enumerate(deciders):
                email = a["email"]
                buttons = [(label, _action_url(base, company, cr_id, action, email), color)
                           for action, label, color in ACTION_META]
                note = _approver_note(deciders, i, is_bh=email.strip().lower() == bh_email,
                                      raised_by=raised_by)
                res = await _send(subject,
                                  _plain_body(detail, buttons=buttons, status_note=note, review_url=review),
                                  _html_body(detail, buttons=buttons, status_note=note, review_url=review),
                                  [email], [], in_reply_to=thread)
                sends.append(([email], res))
                approvers_mailed += 1 if res.get("sent") else 0
            # Everyone else — button-less, in the same thread. Deciders already have
            # their own copy, so drop them here rather than mailing them twice.
            actioners = {a["email"].lower() for a in deciders}
            others_cc = [c for c in rec["cc"] if c.lower() not in actioners]
            others_to = [t for t in rec["to"] if t.lower() not in actioners]
            if others_to or others_cc:
                res = await _send(subject,
                                  _plain_body(detail, buttons=None, status_note=None),
                                  _html_body(detail, buttons=None, status_note=None),
                                  others_to or [routing.get("notify_to")], others_cc,
                                  in_reply_to=thread)
                sends.append(([*others_to, *others_cc], res))
        else:
            # Nobody can decide it (no mapped BH and no deputy, or its only approver
            # raised it) -> a single button-less broadcast, in the same thread.
            res = await _send(subject,
                              _plain_body(detail, buttons=None, status_note=None),
                              _html_body(detail, buttons=None, status_note=None),
                              rec["to"] or [routing.get("notify_to")], rec["cc"], in_reply_to=thread)
            sends.append(([*rec["to"], *rec["cc"]], res))
    except Exception:  # noqa: BLE001 — e.g. no signing key for the links: report, don't 500
        logger.exception("[cr-mail] approval mail failed cr=%s", cr_id)
        sends.append(([], {"sent": False, "reason": "error"}))
    return {**rec, "mail": _mail_outcome(rec, sends, approvers_mailed)}


async def notify_cr_status_changed(conn, detail: dict, new_status: str, actioned_by: str,
                                   remark: str | None = None) -> dict:
    """Threaded reply (no buttons) announcing an Approve/Reject/Hold decision, with
    the decider's remark when there is one. Returns the recipients plus ``mail``."""
    routing = await fetch_routing(conn)
    rec = resolve_recipients(routing, detail)
    cr_id = str(detail.get("rtv_id"))
    note = f"This customer return was marked “{new_status}” by {actioned_by}."
    remark = " ".join(str(remark or "").split())
    if remark:
        note += f" Remark: “{remark}”"
    d = {**detail, "status": new_status}
    res = await _send(_subject(cr_id, reply=True),
                      _plain_body(d, buttons=None, status_note=note),
                      _html_body(d, buttons=None, status_note=note),
                      rec["to"] or [routing.get("notify_to")], rec["cc"],
                      in_reply_to=_thread_key(cr_id))
    return {**rec, "mail": _mail_outcome(rec, [([*rec["to"], *rec["cc"]], res)], 0)}
