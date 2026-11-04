"""Tell the next person when a transfer request moves - by email and WhatsApp.

    event         who                                      WhatsApp template (buttons)
    raised        the store of the supplying (From) site   interunit_transfer_request_store (Accept, Hold)
    accepted      the inventory managers                   interunit_request_approval (Approve, Hold, Reject)
    store_held    the requester and the inventory managers interunit_transfer_request_held
    approved      the requester (created_by)               interunit_request_decision "approved"
    rejected      the requester, with who and why          interunit_request_decision "rejected"
    manager_held  the requester, with who and why          interunit_request_decision "put on hold"
    dispatched    the requester, with challan and vehicle  interunit_request_decision "scanned and sent"

The store's email carries Accept / Hold / Reject and the managers' Approve / Hold /
Reject; the store has no Reject button on WhatsApp (its template has none) and rejects
in the ERP or from the email.

Who
  store       active users who hold transfer.requests.accept through one of their roles
              (roles resolved as auth_service._effective_roles does) and whose warehouse
              lock covers the From site - the rule the store's accept applies
              (permissions.assert_can_answer_request_from: a user without a lock covers
              every site; sites compared as sites, app.core.sites.same_site).
  managers    active users who hold transfer.requests.approve through one of their roles.
  Holding the admin flag alone does not put anyone on these lists (floor requisitions and
  job cards message role holders only), so admins are not messaged about every request.
  Nobody gets a decision message for a request they raised, and the managers' messages
  skip the store user who accepted or held it.
  requester   the request's created_by address, and the WhatsApp number of the active
              user with that email.

The templates (Meta-approved, UTILITY, English, positional) are hard-coded below with
their variables; each has a text header whose {{1}} is the request number. People are
named by their full name when known, else by their email.

Channels, both best-effort
  email     one message per person. The store's and the managers' copies carry links
            signed for that person (request_action_token) to the public page in
            transfer/request_email_router.py. All of a request's mails share one subject
            and reference TRQ-<id>@candorfoods.in, so they thread. Off while SMTP_HOST
            is empty.
  WhatsApp  one template message per number on the shared WABA. The store's and the
            managers' quick replies carry payloads that name the decision
            ('trq:accept:<request id>'), and every one sent is recorded in
            transfer_request_wa_message (migration 126) so a tap that only quotes it is
            matched too; request_wa_reply.py handles the taps.

When
  The request services call notify() right after the action has committed. It schedules
  deliver() as a background task on the app's pool (bind_pool, at startup) and returns at
  once, so a slow mail server or Graph API never delays the response, and nothing here can
  fail or undo the action: every failure is logged and reported in deliver()'s returned
  dict. deliver() reads the request afresh, so a notice asking the store or the managers
  about a request that has already moved on is not sent. Without a bound pool (tests,
  scripts) nothing is sent. Like the app's other background tasks, this runs under the
  persistent server (uvicorn / App Runner).
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from html import escape
from typing import Any, Optional

import httpx

from app.core.mail_identity import Module, SubjectPolicy, stamp
from app.core.sites import canonical_site
from app.core.warehouse_scope import user_has_warehouse
from app.modules.auth.services.phone import normalize as normalize_phone
from app.modules.production.services.wa_notify import IST, current_settings, param, whatsapp_off_reason
from app.modules.transfer.services import query_service
from app.modules.transfer.services.request_action_token import TTL_DAYS, make_token

logger = logging.getLogger(__name__)

EVENTS = ("raised", "accepted", "store_held", "approved", "rejected", "manager_held", "dispatched")
PAYLOAD_PREFIX = "trq"
ACTION_PATH = "/api/v1/transfer/request-email-action"
REQUEST_PAGE = "/modules/transfer/request/{id}"

# The Meta-approved templates. Body variables, in order:
#   store     no, from, to, items, requested by, requested at, reason      Accept, Hold
#   approval  no, from, to, items, requested by, accepted by, accepted at  Approve, Hold, Reject
#   held      no, from, to, items, requested by, held by, held at, reason
#   decision  no, from, to, what ("approved" | "rejected" | "put on hold" | "scanned and
#             sent"), by whom, when, remarks (the reason; "Challan .., vehicle .." for a
#             dispatch; "-" when none)
TEMPLATE_LANG = "en"
TPL_STORE = "interunit_transfer_request_store"
TPL_APPROVAL = "interunit_request_approval"
TPL_HELD = "interunit_transfer_request_held"
TPL_DECISION = "interunit_request_decision"
TEMPLATE_OF_EVENT = {"raised": TPL_STORE, "accepted": TPL_APPROVAL, "store_held": TPL_HELD}

GREEN, ORANGE, RED = "#27ae60", "#e67e22", "#c0392b"
# Who decides at each step: their permission, the status the request waits at, the email
# buttons (action, label, colour) and the WhatsApp quick replies (by template index).
STAGES = {
    "store": {"permission": "accept", "status": "Pending",
              "email_buttons": (("accept", "Accept", GREEN), ("store_hold", "Hold", ORANGE),
                                ("store_reject", "Reject", RED)),
              "wa_buttons": ("accept", "store_hold")},
    "manager": {"permission": "approve", "status": "Accepted",
                "email_buttons": (("approve", "Approve", GREEN), ("hold", "Hold", ORANGE), ("reject", "Reject", RED)),
                "wa_buttons": ("approve", "hold", "reject")},
}
AUDIENCE_OF_EVENT = {"raised": "store", "accepted": "manager", "store_held": "held"}    # else the requester
DECISION_WORD = {"approved": "approved", "rejected": "rejected", "manager_held": "put on hold",
                 "dispatched": "scanned and sent"}
HELD = "Held"

# Variable caps. Meta refuses a template message whose body, filled in, is longer than
# 1024 characters, or a variable that is blank or holds a new line, a tab or 4+ spaces
# (production/wa_notify.param collapses whitespace and never sends blank).
_NO, _SITE, _ITEMS, _NAME, _WHEN, _REASON = 30, 40, 250, 60, 20, 200


# ── the pool and the background task ─────────────────────────────────────────
_pool = None
_tasks: set[asyncio.Task] = set()


def bind_pool(pool) -> None:
    """The pool the notices read and record with (app/main.py, at startup)."""
    global _pool
    _pool = pool


def notify(event: str, request_id: int) -> None:
    """Called by the request services right after `event` has committed. Schedules
    the notice in the background and returns at once. Never raises."""
    coro = None
    try:
        if event not in EVENTS:
            raise ValueError(f"unknown transfer request event {event!r}")
        if _pool is None:
            logger.info("[trq-notify] request %s %s: not sent (no pool bound)", request_id, event)
            return
        coro = deliver(_pool, event, request_id)
        task = asyncio.get_running_loop().create_task(coro)
        _tasks.add(task)    # a strong reference until it finishes (asyncio keeps weak ones)
        task.add_done_callback(_tasks.discard)
    except Exception:  # noqa: BLE001 - a notice must never fail the action
        if coro is not None:
            coro.close()
        logger.exception("[trq-notify] request %s %s: could not schedule the notice", request_id, event)


async def drain() -> None:
    """Wait until the notices in flight have finished (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ── who ──────────────────────────────────────────────────────────────────────
# One row per active user holding transfer.requests.<$1> through one of their roles:
# auth_user_role is the source of truth, and the primary auth_user.role_id counts only
# for a user with no rows there (auth_service._effective_roles). The permission may be
# granted at (transfer, requests) or, as check_permission falls back, at (transfer, -).
# The tap or link re-checks with check_permission itself.
HOLDERS_SQL = """
    SELECT u.user_id, u.full_name, u.email, u.phone, u.allowed_warehouses
      FROM auth_user u
     WHERE u.is_active
       AND COALESCE(u.status, 'active') = 'active'
       AND EXISTS (
             SELECT 1
               FROM auth_role_permission rp
               JOIN auth_permission p ON p.permission_id = rp.permission_id
              WHERE p.module = 'transfer'
                AND (p.sub_module = 'requests' OR p.sub_module IS NULL)
                AND p.sub_sub_module IS NULL
                AND p.action = $1
                AND (rp.role_id IN (SELECT ur.role_id FROM auth_user_role ur WHERE ur.user_id = u.user_id)
                     OR (rp.role_id = u.role_id
                         AND NOT EXISTS (SELECT 1 FROM auth_user_role ur WHERE ur.user_id = u.user_id))))
     ORDER BY u.full_name, u.user_id
"""


def _email_key(value: Optional[str]) -> str:
    return (value or "").strip().lower()


async def permission_holders(conn, permission: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(HOLDERS_SQL, permission)
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        out.setdefault(r["user_id"], {
            "user_id": r["user_id"], "name": (r["full_name"] or "").strip(),
            "email": (r["email"] or "").strip(), "phone": (r["phone"] or "").strip(),
            "allowed_warehouses": list(r["allowed_warehouses"] or [])})
    return list(out.values())


def _is(person: dict[str, Any], email: Optional[str]) -> bool:
    """Whether `person` is the account recorded as `email` on the request. An account
    with no email is recorded as 'unknown', and two such accounts cannot be told apart
    (permissions.assert_not_own_request), so 'unknown' matches every one of them."""
    recorded = _email_key(email)
    if not recorded:
        return False
    mine = _email_key(person.get("email")) or "unknown"
    return mine == recorded


def store_team(holders: list[dict[str, Any]], req: dict[str, Any]) -> list[dict[str, Any]]:
    """The holders of transfer.requests.accept who may answer for the request's From
    site, without the person who raised it."""
    from_site = req.get("from_warehouse")
    return [h for h in holders
            if (not h["allowed_warehouses"] or user_has_warehouse(h["allowed_warehouses"], from_site))
            and not _is(h, req.get("created_by"))]


def managers(holders: list[dict[str, Any]], req: dict[str, Any]) -> list[dict[str, Any]]:
    """The holders of transfer.requests.approve, without the person who raised the
    request or the store user who answered it (accepted or held it)."""
    return [h for h in holders
            if not _is(h, req.get("created_by")) and not _is(h, req.get("store_decided_by"))]


async def _requester(conn, req: dict[str, Any]) -> Optional[dict[str, Any]]:
    email = (req.get("created_by") or "").strip()
    if "@" not in email:
        return None
    row = await conn.fetchrow(
        "SELECT user_id, full_name, phone FROM auth_user "
        "WHERE lower(btrim(email)) = lower($1) AND is_active AND COALESCE(status, 'active') = 'active' "
        "ORDER BY user_id LIMIT 1", email)
    return {"user_id": row["user_id"] if row else None,
            "name": ((row["full_name"] if row else "") or "").strip(),
            "email": email, "phone": ((row["phone"] if row else "") or "").strip()}


async def _names(conn, emails) -> dict[str, str]:
    keys = sorted({_email_key(e) for e in emails if "@" in (e or "")})
    if not keys:
        return {}
    rows = await conn.fetch(
        "SELECT lower(btrim(email)) AS email, full_name FROM auth_user "
        "WHERE lower(btrim(email)) = ANY($1::text[])", keys)
    return {r["email"]: (r["full_name"] or "").strip() for r in rows if (r["full_name"] or "").strip()}


# ── what ─────────────────────────────────────────────────────────────────────
def _num(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value or "").strip()
    if f != f or f in (float("inf"), float("-inf")):
        return ""
    return str(int(f)) if f == int(f) else f"{f:.3f}".rstrip("0").rstrip(".")


def item_text(line: dict[str, Any]) -> str:
    """'PISTA - qty 2 KG, 50 kg net', as the request page shows a line (emails, pages)."""
    text = (line.get("item_description") or "").strip() or "-"
    qty, uom, net = _num(line.get("quantity")), (line.get("uom") or "").strip(), _num(line.get("net_weight"))
    if qty and qty != "0":
        text += f" - qty {qty}" + (f" {uom}" if uom else "")
    if net and net != "0":
        text += f", {net} kg net"
    return text


def _item_short(line: dict[str, Any]) -> str:
    desc = (line.get("item_description") or "").strip() or "-"
    qty = _num(line.get("quantity"))
    return f"{desc} ({qty})" if qty and qty != "0" else desc


def items_line(lines: list[dict[str, Any]], limit: int) -> str:
    """The items as the templates show them: 'Cashew W320 (10), Almond 25kg (4) +2 more'
    - what does not fit is counted."""
    texts = [_item_short(l) for l in lines] or ["-"]
    for shown in range(len(texts), 0, -1):
        more = len(texts) - shown
        text = ", ".join(texts[:shown]) + (f" +{more} more" if more else "")
        if len(text) <= limit:
            return text
    tail = f" +{len(texts) - 1} more" if len(texts) > 1 else ""
    return texts[0][: max(1, limit - len(tail) - 1)].rstrip() + "…" + tail


def when(value: Any) -> str:
    """'01 Oct 2026, 11:20'. The request's times are stored without a zone, as the ERP's
    server wrote them, and the ERP's pages show them as they are; so does this. A time
    with a zone is shown in IST."""
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(IST)
        return value.strftime("%d %b %Y, %H:%M")
    return str(value)


def _who(email: Optional[str], names: dict[str, str]) -> str:
    email = (email or "").strip()
    if not email:
        return "-"
    name = names.get(_email_key(email))
    return f"{name} ({email})" if name and "@" in email else (name or email)


def _short(email: Optional[str], names: dict[str, str]) -> str:
    """A person by their full name when known, else by the address."""
    email = (email or "").strip()
    return names.get(_email_key(email)) or email or "-"


def _text(value: Any) -> str:
    return (str(value) if value is not None else "").strip() or "-"


def facts(req: dict[str, Any], names: dict[str, str], transfer: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """Everything a notice may say about the request, as display strings."""
    rid = req.get("id")
    t = transfer or {}
    return {
        "id": str(rid),
        "no": (req.get("request_no") or "").strip() or f"#{rid}",
        "date": req.get("request_date") or "-",
        "from": canonical_site(req.get("from_warehouse")) or "-",
        "to": canonical_site(req.get("to_warehouse")) or "-",
        "reason": _text(req.get("reason_description")),
        "raised_by": _who(req.get("created_by"), names),
        "requested_by": _short(req.get("created_by"), names),
        "requested_at": when(req.get("created_ts")),
        "store_by": _short(req.get("store_decided_by"), names),
        "store_at": when(req.get("store_decided_at")),
        "store_reject_reason": _text(req.get("store_reject_reason")),
        "store_hold_reason": _text(req.get("store_hold_reason")),
        "manager_by": _short(req.get("manager_decided_by"), names),
        "manager_at": when(req.get("manager_decided_at")),
        "manager_reject_reason": _text(req.get("manager_reject_reason")),
        "manager_hold_reason": _text(req.get("manager_hold_reason")),
        "challan": (t.get("challan_no") or req.get("transfer_challan_no") or "").strip() or "-",
        "sent_by": _short(t.get("created_by"), names),
        "sent_at": when(t.get("created_ts")),
        "vehicle": (t.get("vehicle_no") or "").strip(),
    }


def decision(event: str, req: dict[str, Any], f: dict[str, str]) -> tuple[str, str, str, str, str]:
    """(what, by whom, when, remarks, one sentence) of a decision the requester is told."""
    word = DECISION_WORD[event]
    if event == "approved":
        return word, f["manager_by"], f["manager_at"], "-", \
            f"Approved by {f['manager_by']}. The store at {f['from']} can now send it."
    if event == "manager_held":
        return word, f["manager_by"], f["manager_at"], f["manager_hold_reason"], \
            f"Put on hold by the inventory manager {f['manager_by']}. Reason: {f['manager_hold_reason']}"
    if event == "dispatched":
        remarks = f"Challan {f['challan']}" + (f", vehicle {f['vehicle']}" if f["vehicle"] else "")
        return word, f["sent_by"], f["sent_at"], remarks, \
            f"Scanned and sent from {f['from']} by {f['sent_by']}: {remarks}."
    if (req.get("manager_decision") or "") == "Rejected":
        return word, f["manager_by"], f["manager_at"], f["manager_reject_reason"], \
            f"Rejected by the inventory manager {f['manager_by']}. Reason: {f['manager_reject_reason']}"
    return word, f["store_by"], f["store_at"], f["store_reject_reason"], \
        f"Rejected by the store at {f['from']} ({f['store_by']}). Reason: {f['store_reject_reason']}"


# ── email ────────────────────────────────────────────────────────────────────
def thread_key(request_id: Any) -> str:
    return f"TRQ-{request_id}@candorfoods.in"


def subject(f: dict[str, str]) -> str:
    """One subject for every mail about a request, so they thread (ANCHOR policy)."""
    return f"Transfer request {f['no']}: {f['from']} to {f['to']}"


def action_url(base: str, request_id: Any, action: str, person: dict[str, Any]) -> str:
    token = make_token(int(request_id), action, int(person["user_id"]), person["email"])
    return f"{(base or '').rstrip('/')}{ACTION_PATH}?token={token}"


def _heading_and_note(event: str, req: dict[str, Any], f: dict[str, str]) -> tuple[str, str]:
    if event == "raised":
        return (f"Transfer request {f['no']} needs the store's answer",
                f"{f['to']} has asked {f['from']} to send the items below. Accept to agree to send them, "
                "Hold to keep it waiting, or Reject to refuse it. Hold and Reject ask for the reason.")
    if event == "accepted":
        return (f"Transfer request {f['no']} needs an inventory manager's approval",
                f"The store at {f['from']} has accepted it ({f['store_by']}). Approve it so it can be sent, "
                "Hold it, or Reject it. Hold and Reject ask for the reason.")
    if event == "store_held":
        return (f"Transfer request {f['no']} is on hold by the store",
                f"The store at {f['from']} ({f['store_by']}) has put it on hold. Reason: {f['store_hold_reason']}. "
                "Please follow up with the store team. The request stays open until it is accepted or deleted.")
    word, _, _, _, sentence = decision(event, req, f)
    return f"Your transfer request {f['no']} has been {word}", sentence


def _info_rows(event: str, req: dict[str, Any], f: dict[str, str]) -> list[tuple[str, str]]:
    rows = [("Request no", f["no"]), ("Request date", f["date"]),
            ("Requesting site (to)", f["to"]), ("Supplying site (from)", f["from"]),
            ("Reason", f["reason"]), ("Raised by", f["raised_by"]), ("Raised at", f["requested_at"])]
    if req.get("store_decision") == "Accepted":
        rows.append(("Accepted by the store", f"{f['store_by']}, {f['store_at']}"))
    if req.get("store_decision") == HELD:
        rows += [("On hold by the store", f"{f['store_by']}, {f['store_at']}"),
                 ("Reason for the hold", f["store_hold_reason"])]
    if event in DECISION_WORD and event != "dispatched":
        word, by, at, remarks, _ = decision(event, req, f)
        rows.append((f"{word[0].upper()}{word[1:]} by", f"{by}, {at}"))
        if remarks != "-":
            rows.append(("Reason for the hold" if event == "manager_held" else "Reason for rejecting", remarks))
    if event == "dispatched":
        rows += [("Challan no", f["challan"]), ("Sent by", f"{f['sent_by']}, {f['sent_at']}")]
        if f["vehicle"]:
            rows.append(("Vehicle", f["vehicle"]))
    return rows


def email_body(event: str, req: dict[str, Any], f: dict[str, str], web_url: str,
               buttons: Optional[list[tuple[str, str, str]]] = None) -> tuple[str, str]:
    """(plain, html) for one recipient. `buttons` = [(label, url, colour)] on the
    store's and the managers' copies only."""
    heading, note = _heading_and_note(event, req, f)
    rows = _info_rows(event, req, f)
    lines = req.get("lines") or []
    erp = f"{(web_url or '').rstrip('/')}{REQUEST_PAGE.format(id=f['id'])}"
    help_text = (f"{buttons[0][0] if buttons else 'Accept'} opens a page to confirm; Hold and Reject open a "
                 f"page that asks for the reason. These links are for you only, work for {TTL_DAYS} days, and "
                 "do nothing once the request has been decided.")

    plain = [heading, "", note, ""]
    plain += [f"{k}: {v}" for k, v in rows]
    plain += ["", f"Items ({len(lines)}):"] + [f"  - {item_text(l)}" for l in lines]
    if buttons:
        plain += ["", "Answer it:"] + [f"  {label}: {url}" for label, url, _ in buttons] + ["", help_text]
    plain += ["", f"Open it in the ERP: {erp}"]

    info = "".join(
        f'<tr><td style="padding:3px 12px 3px 0;color:#555;">{escape(k)}</td>'
        f'<td style="padding:3px 0;font-weight:600;">{escape(str(v))}</td></tr>' for k, v in rows)
    cell = 'style="padding:4px 8px;border-top:1px solid #eee;'
    item_rows = "".join(
        f'<tr><td {cell}">{escape(l.get("item_description") or "-")}</td>'
        f'<td {cell}text-align:right;">{escape(_num(l.get("quantity")) or "-")}</td>'
        f'<td {cell}">{escape(l.get("uom") or "-")}</td>'
        f'<td {cell}text-align:right;">{escape(_num(l.get("net_weight")) or "-")}</td></tr>' for l in lines)
    buttons_html = ""
    if buttons:
        cells = "".join(
            f'<a href="{escape(url)}" style="display:inline-block;padding:10px 22px;margin:4px 5px;'
            f'background:{color};color:#fff;text-decoration:none;font-weight:bold;border-radius:6px;'
            f'font-size:14px;">{escape(label)}</a>' for label, url, color in buttons)
        buttons_html = (f'<div style="text-align:center;margin:16px 0 8px;">{cells}</div>'
                        f'<p style="text-align:center;font-size:11px;color:#999;margin:0 0 18px;">{escape(help_text)}</p>')
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;color:#222;">'
        f'<h2 style="margin:0 0 8px;">{escape(heading)}</h2>'
        f'<p style="font-size:14px;color:#333;margin:0 0 12px;">{escape(note)}</p>'
        f'{buttons_html}'
        f'<table style="border-collapse:collapse;font-size:14px;margin-bottom:16px;">{info}</table>'
        f'<h3 style="font-size:14px;margin:0 0 6px;">Items ({len(lines)})</h3>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px;">'
        '<tr style="text-align:left;color:#555;"><th style="padding:4px 8px;">Item</th>'
        '<th style="padding:4px 8px;text-align:right;">Qty</th><th style="padding:4px 8px;">UOM</th>'
        f'<th style="padding:4px 8px;text-align:right;">Net kg</th></tr>{item_rows}</table>'
        f'<p style="font-size:13px;margin:18px 0 0;"><a href="{escape(erp)}">Open it in the ERP</a></p>'
        '</div>')
    return "\n".join(plain), html


def _send_mail_sync(subject_line: str, plain: str, html: str, to_addr: str, *,
                    thread: Optional[str] = None, entity_id: Optional[str] = None,
                    event: Optional[str] = None) -> bool:
    """One mail. True only when the SMTP server accepted it; never raises."""
    s = current_settings()
    host = (s.SMTP_HOST or "").strip()
    sender = (s.SMTP_EMAIL or "").strip()
    if not host:
        return False
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject_line
    if thread:
        # Every mail about a request refers to the same thread id (none claims it as its
        # own Message-ID: a request can be raised again after an edit, and a mailbox drops
        # a second message with a Message-ID it already has).
        msg["In-Reply-To"] = f"<{thread}>"
        msg["References"] = f"<{thread}>"
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    # ANCHOR: every mail about a request keeps the same subject, so they thread.
    stamp(msg, module=Module.TRANSFERS, policy=SubjectPolicy.ANCHOR, entity_type="TransferRequest",
          entity_id=entity_id, event=event, status=event, sender=sender)
    try:
        with smtplib.SMTP(host, s.SMTP_PORT, timeout=20) as srv:
            srv.starttls(context=ssl.create_default_context())
            if sender:
                srv.login(sender, (s.SMTP_APP_PASSWORD or "").strip())
            srv.send_message(msg, from_addr=sender, to_addrs=[to_addr])
        return True
    except Exception:  # noqa: BLE001 - best-effort
        logger.exception("[trq-notify] mail to %s failed (subject=%r)", to_addr, subject_line)
        return False


async def _send_mail(*args, **kwargs) -> bool:
    """_send_mail_sync off the event loop (smtplib blocks)."""
    return await asyncio.to_thread(_send_mail_sync, *args, **kwargs)


# ── WhatsApp ─────────────────────────────────────────────────────────────────
def button_payload(action: str, request_id: Any) -> str:
    """What a quick-reply tap sends back: 'trq:accept:41250001'."""
    return f"{PAYLOAD_PREFIX}:{action}:{request_id}"


def header_params(f: dict[str, str]) -> list[str]:
    """Every template's text header: {{1}} = the request number."""
    return [param(f["no"], _NO)]


def body_params(event: str, req: dict[str, Any], f: dict[str, str]) -> list[str]:
    """The event's template body variables, in the template's order (see the table at
    the top of the module)."""
    head = [param(f["no"], _NO), param(f["from"], _SITE), param(f["to"], _SITE)]
    items = param(items_line(req.get("lines") or [], _ITEMS), _ITEMS)
    if event == "raised":
        return head + [items, param(f["requested_by"], _NAME), param(f["requested_at"], _WHEN),
                       param(f["reason"], _REASON)]
    if event == "accepted":
        return head + [items, param(f["requested_by"], _NAME), param(f["store_by"], _NAME),
                       param(f["store_at"], _WHEN)]
    if event == "store_held":
        return head + [items, param(f["requested_by"], _NAME), param(f["store_by"], _NAME),
                       param(f["store_at"], _WHEN), param(f["store_hold_reason"], _REASON)]
    word, by, at, remarks, _ = decision(event, req, f)
    return head + [param(word, 20), param(by, _NAME), param(at, _WHEN), param(remarks, _REASON)]


def template_message(name: str, to: str, header: list[str], body: list[str],
                     buttons: tuple[str, ...] = ()) -> dict[str, Any]:
    components: list[dict[str, Any]] = [
        {"type": "header", "parameters": [{"type": "text", "text": p} for p in header]},
        {"type": "body", "parameters": [{"type": "text", "text": p} for p in body]}]
    for i, payload in enumerate(buttons):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                           "parameters": [{"type": "payload", "payload": payload}]})
    return {"messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": name, "language": {"code": TEMPLATE_LANG}, "components": components}}


async def _post_template(settings, message: dict[str, Any]) -> Optional[str]:
    """Send one message on the shared WABA (production/wa_notify.send_template's call)
    and return its wamid. Raises on an HTTP error."""
    base = (settings.WHATSAPP_GRAPH_BASE or "https://graph.facebook.com/v21.0").rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{base}/{settings.WHATSAPP_PHONE_NUMBER_ID.strip()}/messages",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN.strip()}",
                     "Content-Type": "application/json"},
            json=message)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} - {resp.text[:200]}")
    try:
        return ((resp.json() or {}).get("messages") or [{}])[0].get("id")
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


def _wa_number(phone: Optional[str]) -> str:
    raw = (phone or "").strip()
    return (normalize_phone(raw) or raw).lstrip("+") if raw else ""


async def _record_sent(pool, rows: list[tuple]) -> None:
    """Remember who was sent which message, so a tap quoting it is matched. Best-effort:
    the payload of each button names its request anyway."""
    if not rows:
        return
    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO transfer_request_wa_message (wamid, request_id, stage, user_id, wa_phone) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (wamid) DO NOTHING", rows)
    except Exception:  # noqa: BLE001 - migration 126 not applied, or a transient error
        logger.exception("[trq-notify] could not record %d sent message(s)", len(rows))


# ── the notice ───────────────────────────────────────────────────────────────
def _still_due(event: str, req: dict[str, Any]) -> bool:
    """Whether the request is still where the notice says it is. Only the notices that
    ask someone to act (or to follow up on a hold) are dropped when it has moved on; the
    requester is told of every decision."""
    status = req.get("status") or "Pending"
    if event == "raised":
        return status == "Pending"
    if event == "accepted":
        return status == "Accepted"
    if event == "store_held":
        return status == "Pending" and req.get("store_decision") == HELD
    return True


async def _plan(conn, event: str, req: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Who gets this notice and what it says; None when it is not to be sent."""
    if not _still_due(event, req):
        return None
    audience = AUDIENCE_OF_EVENT.get(event, "requester")
    transfer = None
    if audience in STAGES:
        people = (store_team if audience == "store" else managers)(
            await permission_holders(conn, STAGES[audience]["permission"]), req)
    else:
        requester = await _requester(conn, req)
        people = [requester] if requester else []
        if audience == "held":
            people += managers(await permission_holders(conn, "approve"), req)
        if event == "dispatched":
            transfer = (await query_service.newest_transfers(conn, [req["id"]])).get(req["id"])
    names = await _names(conn, [req.get("created_by"), req.get("store_decided_by"),
                                req.get("manager_decided_by"), (transfer or {}).get("created_by"),
                                *(p.get("email") for p in people)])
    return {"audience": audience, "people": people, "facts": facts(req, names, transfer)}


async def deliver(pool, event: str, request_id: int) -> dict[str, Any]:
    """Send the notice for `event` on request `request_id`. Never raises; returns what
    was sent, skipped and failed (also logged)."""
    report: dict[str, Any] = {"event": event, "request_id": request_id, "recipients": [],
                              "email": {"sent": [], "skipped": None, "failed": []},
                              "whatsapp": {"sent": [], "skipped": None, "failed": []},
                              "error": None}
    try:
        # Read what is needed, then give the connection back before any network I/O.
        async with pool.acquire() as conn:
            req = await query_service.get_request(conn, request_id)
            plan = await _plan(conn, event, req)
        if plan is None:
            report["email"]["skipped"] = report["whatsapp"]["skipped"] = "request_moved_on"
            logger.info("[trq-notify] request %s %s: now %s - notice not sent", request_id, event, req.get("status"))
            return report
        audience, people, f = plan["audience"], plan["people"], plan["facts"]
        report["recipients"] = [p.get("name") or p.get("email") or p.get("phone") for p in people]
        if not people:
            report["email"]["skipped"] = report["whatsapp"]["skipped"] = "no_recipients"
            log = logger.warning if audience in STAGES else logger.info
            log("[trq-notify] request %s %s: nobody to tell (%s)", request_id, event,
                f"no store user for {f['from']}" if audience == "store" else audience)
            return report
        settings = current_settings()
        await _emails(settings, event, req, plan, report)
        sent = await _whatsapps(settings, event, req, plan, report)
        if audience in STAGES:
            await _record_sent(pool, [(wamid, int(request_id), audience, uid, to) for wamid, uid, to in sent if wamid])
    except Exception as exc:  # noqa: BLE001 - a notice must never surface anywhere
        logger.exception("[trq-notify] request %s %s: notice failed", request_id, event)
        report["error"] = str(getattr(exc, "detail", None) or exc)
    logger.info("[trq-notify] request %s %s: %s", request_id, event, report)
    return report


async def _emails(settings, event: str, req: dict[str, Any], plan: dict[str, Any], report: dict[str, Any]) -> None:
    audience, f = plan["audience"], plan["facts"]
    by_address: dict[str, dict[str, Any]] = {}
    for p in plan["people"]:
        if "@" in (p.get("email") or ""):
            by_address.setdefault(_email_key(p["email"]), p)    # one mail per address
    people = list(by_address.values())
    if not people:
        report["email"]["skipped"] = "no_email_on_recipients"
        return
    if not str(settings.SMTP_HOST or "").strip():
        report["email"]["skipped"] = "smtp_not_configured"
        return
    base = (settings.PUBLIC_BACKEND_URL or "").strip()
    for person in people:
        buttons = None
        if audience in STAGES and person.get("user_id") is not None:
            buttons = [(label, action_url(base, f["id"], action, person), color)
                       for action, label, color in STAGES[audience]["email_buttons"]]
        plain, html = email_body(event, req, f, settings.WEB_APP_URL, buttons)
        try:
            ok = await _send_mail(subject(f), plain, html, person["email"],
                                  thread=thread_key(f["id"]), entity_id=f["no"], event=event)
        except Exception as exc:  # noqa: BLE001 - one bad address must not stop the rest
            logger.warning("[trq-notify] mail to %s failed: %s", person["email"], exc)
            ok = False
        (report["email"]["sent"] if ok else report["email"]["failed"]).append(person["email"])


async def _whatsapps(settings, event: str, req: dict[str, Any], plan: dict[str, Any],
                     report: dict[str, Any]) -> list[tuple]:
    """[(wamid, user_id, number)] of the messages sent."""
    audience, f = plan["audience"], plan["facts"]
    off = whatsapp_off_reason(settings)
    if off:
        report["whatsapp"]["skipped"] = off
        return []
    numbers: dict[str, dict[str, Any]] = {}
    for p in plan["people"]:
        to = _wa_number(p.get("phone"))
        if to:
            numbers.setdefault(to, p)    # one message per number, however many accounts share it
    if not numbers:
        report["whatsapp"]["skipped"] = "no_phone_on_recipients"
        return []
    name = TEMPLATE_OF_EVENT.get(event, TPL_DECISION)
    buttons = tuple(button_payload(a, f["id"]) for a in STAGES[audience]["wa_buttons"]) if audience in STAGES else ()
    header, body = header_params(f), body_params(event, req, f)
    sent: list[tuple] = []
    for to, person in numbers.items():
        try:
            wamid = await _post_template(settings, template_message(name, to, header, body, buttons))
            sent.append((wamid, person.get("user_id"), to))
            report["whatsapp"]["sent"].append(to)
        except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
            logger.warning("[trq-notify] WhatsApp to %s failed: %s", to, exc)
            report["whatsapp"]["failed"].append({"phone": to, "error": str(exc)})
    return sent
