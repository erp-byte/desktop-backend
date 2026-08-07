"""Customer-Returns approval e-mail (threaded, HTML + plain, magic-link buttons).

Faithful to the legacy shared/email_notifier.py RTV flow, adapted to the live
async backend:
  * deterministic thread id ``RTV-<cr_id>@candorfoods.in`` — set as Message-ID on
    the "created" mail, as In-Reply-To + References on every later mail. No DB
    column (pure function of the id), so replies always resolve to the root.
  * only an APPROVER's copy of the "created" mail carries the Approve / Reject /
    Hold buttons (signed magic links -> the public /email-action endpoint);
    everyone else gets a button-less copy threaded under the root.
  * an approver is the CR's Business Head PLUS the standing deputies configured as
    ``deputy_approver`` in cr_email_routing. A return must close the day it is
    raised, and one named BU Head is a single point of failure. Each approver gets
    their OWN copy carrying a token minted for their own address, so the audit
    records who actually decided rather than crediting the BU Head for a deputy's
    click. The decision itself is idempotent, so two approvers acting at once is
    safe — the first wins, the second is told it is already actioned.
  * recipient routing comes from the DB (fetch_routing): BH/sales POC by role
    (auth_user), warehouse-owner / constant CC / notify-to from cr_email_routing.
  * best-effort: a missing SMTP config or a send failure is logged and swallowed
    so it never fails the API request (mirrors production/mail_service).
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape

from app.config import Settings
from app.core.mail_identity import Module, SubjectPolicy, stamp
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
    if not factory_unit:
        return None
    low = str(factory_unit).strip().lower()
    code = low.replace("-", "").replace(" ", "")
    for r in routing.get("warehouse_cc", []):
        key = (r.get("match_key") or "").strip().lower()
        if not key:
            continue
        if r.get("match_type") == "substring" and key in low:
            return r["email"]
        if r.get("match_type") == "code" and code == key.replace("-", "").replace(" ", ""):
            return r["email"]
    return None


def resolve_recipients(routing: dict, header: dict) -> dict:
    """{approver, approvers, to, cc} — port of the FE _approvalMatrix.resolveRecipients.

    The BH (if mapped) and the standing deputies are the approvers; they lead TO
    alongside notify_to. CC = sales POC (mapped + manual) + constants + creator +
    warehouse owner, deduped, with the TO addresses removed.

    ``approver`` stays the PRIMARY alone — it is what the UI labels "Business Head"
    and what the response payload has always meant. ``approvers`` is the full list
    that actually receives buttons, primary first.
    """
    bh_email = lookup_business_head_email(routing, header.get("business_head"))
    approver = {"name": (header.get("business_head") or "").strip() or bh_email, "email": bh_email} if bh_email else None

    approvers: list[dict] = [approver] if approver else []
    for d in routing.get("deputy_approver", []):
        email = (d.get("email") or "").strip()
        if email and email.lower() not in {a["email"].strip().lower() for a in approvers}:
            approvers.append({"name": (d.get("name") or "").strip() or email, "email": email})

    to = [e for e in [*(a["email"] for a in approvers), routing.get("notify_to")] if e]
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
    return {"approver": approver, "approvers": approvers, "to": to, "cc": cc}


# ── SMTP send (best-effort, off the event loop) ───────────────────────────────
def _send_sync(subject: str, plain: str, html: str, to_addrs: list[str], cc_addrs: list[str],
               message_id: str | None, in_reply_to: str | None) -> None:
    s = Settings()
    host = (s.SMTP_HOST or "").strip()
    sender = (s.SMTP_EMAIL or "").strip()
    if not host:
        logger.info("[cr-mail] SMTP_HOST not configured — skipping (subject=%r)", subject)
        return
    # Drop None / blank recipients (e.g. an unseeded notify_to before the routing
    # migration is applied) — a best-effort mail must never raise. Promote CC to To
    # if no explicit To survives, and skip silently if nothing is left.
    to_addrs = [a.strip() for a in (to_addrs or []) if a and str(a).strip()]
    cc_addrs = [a.strip() for a in (cc_addrs or []) if a and str(a).strip()]
    if not to_addrs:
        to_addrs, cc_addrs = cc_addrs, []
    if not to_addrs:
        logger.info("[cr-mail] no recipients resolvable — skipping (subject=%r)", subject)
        return
    recipients = list(dict.fromkeys([*to_addrs, *cc_addrs]))
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = f"<{message_id}>"
    if in_reply_to:
        msg["In-Reply-To"] = f"<{in_reply_to}>"
        msg["References"] = f"<{in_reply_to}>"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    # ANCHOR: customer returns thread one conversation per CR, and that depends on
    # every mail sharing the exact subject. Only the constant module glyph is added.
    stamp(msg, module=Module.RETURNS, policy=SubjectPolicy.ANCHOR,
          entity_type="CustomerReturn", sender=sender)

    try:
        with smtplib.SMTP(host, s.SMTP_PORT, timeout=20) as srv:
            srv.starttls(context=ssl.create_default_context())
            if sender:
                srv.login(sender, (s.SMTP_APP_PASSWORD or "").strip())
            srv.send_message(msg, from_addr=sender, to_addrs=recipients)
        logger.info("[cr-mail] sent subject=%r to=%s cc=%s", subject, to_addrs, cc_addrs)
    except Exception:
        logger.exception("[cr-mail] send failed subject=%r to=%s", subject, to_addrs)


async def _send(subject, plain, html, to_addrs, cc_addrs, *, message_id=None, in_reply_to=None) -> None:
    await asyncio.to_thread(_send_sync, subject, plain, html, to_addrs, cc_addrs, message_id, in_reply_to)


# ── body builders ─────────────────────────────────────────────────────────────
def _action_url(base: str, company: str, cr_id: str, action: str, approver_email: str) -> str:
    token = make_action_token(company, cr_id, action, approver_email)
    return f"{base.rstrip('/')}/api/v1/customer-returns/email-action?token={token}"


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


def _html_body(detail: dict, *, buttons: list[tuple[str, str, str]] | None, status_note: str | None) -> str:
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
            f'One-click — no login needed. Link expires in 14 days.</p>'
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


def _plain_body(detail: dict, *, buttons: list[tuple[str, str, str]] | None, status_note: str | None) -> str:
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
        lines += ["", "Actions (one-click, no login; expires in 14 days):"]
        lines += [f"  {label}: {url}" for label, url, _ in buttons]
    return "\n".join(lines)


def _approver_note(approvers: list[dict], me: int) -> str:
    """The line above the buttons on an approver's copy.

    It has to say the other approvers exist, or the deputies read their copy as a
    mistake and wait for the BU Head — which is the delay this whole change is
    meant to remove. It also has to say the first decision wins, so nobody holds
    off in case someone else is mid-click.
    """
    others = [a["name"] for i, a in enumerate(approvers) if i != me]
    if not others:
        return "Please Approve / Reject / Hold this customer return using the buttons below."
    who = others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]
    role = "You are the assigned Business Head." if me == 0 else "You are a standing deputy approver."
    return (f"Please Approve / Reject / Hold this customer return today. {role} "
            f"{who} can also action it — whoever decides first is the decision.")


def _subject(cr_id: str, *, reply: bool) -> str:
    base = f"Customer Returns Created: {cr_id}"
    return f"Re: {base}" if reply else base


# ── public notifiers ──────────────────────────────────────────────────────────
async def notify_cr_created(conn, detail: dict) -> dict:
    """Send the approval mail: one copy per approver WITH Approve/Reject/Hold
    buttons, plus a button-less copy to notify_to + CC. Returns the resolved
    recipients for the caller to surface. Best-effort send."""
    routing = await fetch_routing(conn)
    rec = resolve_recipients(routing, detail)
    cr_id = str(detail.get("rtv_id"))
    company = str(detail.get("company") or "")
    thread = _thread_key(cr_id)
    subject = _subject(cr_id, reply=False)
    base = (Settings().PUBLIC_BACKEND_URL or "").strip()

    if rec["approvers"]:
        approver_emails = [a["email"] for a in rec["approvers"]]
        # Threading is unchanged: the FIRST approver's copy still claims the thread
        # root Message-ID and every later mail — deputy copies, the broadcast, the
        # eventual status mail — replies into it. One conversation per CR, as before.
        for i, email in enumerate(approver_emails):
            buttons = [(label, _action_url(base, company, cr_id, action, email), color)
                       for action, label, color in ACTION_META]
            note = _approver_note(rec["approvers"], i)
            await _send(subject,
                        _plain_body(detail, buttons=buttons, status_note=note),
                        _html_body(detail, buttons=buttons, status_note=note),
                        [email], [],
                        message_id=thread if i == 0 else None,
                        in_reply_to=None if i == 0 else thread)
        # Everyone else — button-less, threaded under the root. Approvers already
        # have their own copy, so drop them here rather than mailing them twice.
        actioners = {e.lower() for e in approver_emails}
        others_cc = [c for c in rec["cc"] if c.lower() not in actioners]
        others_to = [t for t in rec["to"] if t.lower() not in actioners]
        if others_to or others_cc:
            await _send(subject,
                        _plain_body(detail, buttons=None, status_note=None),
                        _html_body(detail, buttons=None, status_note=None),
                        others_to or [routing.get("notify_to")], others_cc,
                        in_reply_to=thread)
    else:
        # No mapped BH -> single broadcast, no buttons, as the thread root.
        await _send(subject,
                    _plain_body(detail, buttons=None, status_note=None),
                    _html_body(detail, buttons=None, status_note=None),
                    rec["to"] or [routing.get("notify_to")], rec["cc"], message_id=thread)
    return rec


async def notify_cr_status_changed(conn, detail: dict, new_status: str, actioned_by: str) -> dict:
    """Threaded reply (no buttons) announcing an Approve/Reject/Hold decision."""
    routing = await fetch_routing(conn)
    rec = resolve_recipients(routing, detail)
    cr_id = str(detail.get("rtv_id"))
    note = f"This customer return was marked “{new_status}” by {actioned_by}."
    d = {**detail, "status": new_status}
    await _send(_subject(cr_id, reply=True),
                _plain_body(d, buttons=None, status_note=note),
                _html_body(d, buttons=None, status_note=note),
                rec["to"] or [routing.get("notify_to")], rec["cc"],
                in_reply_to=_thread_key(cr_id))
    return rec
