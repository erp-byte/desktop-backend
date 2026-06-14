"""Threaded review email for the NPD sample flow — ports transfer_backend_reference's
email_notifier pattern onto Settings().SMTP_*. Best-effort: never raises into the
lifecycle. Recipients resolve from auth_user (email + role), NOT the broken users join."""
from __future__ import annotations
import hashlib
import html as _html
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import quote

from app.config import Settings

logger = logging.getLogger(__name__)

REMINDER_MIN_HOURS = 24
REMINDER_MAX = 5


async def _emails_for_role(conn, role: str) -> list[str]:
    rows = await conn.fetch(
        """SELECT a.email FROM auth_user a JOIN auth_role r ON a.role_id = r.role_id
            WHERE r.role_name = $1 AND COALESCE(a.is_active, TRUE)
              AND a.email IS NOT NULL AND btrim(a.email) <> ''""", role)
    return [r["email"] for r in rows]


async def _email_for_user(conn, uid) -> str | None:
    if not uid:
        return None
    return await conn.fetchval("SELECT email FROM auth_user WHERE user_id = $1", uid)


def _send(subject, html, to_addrs, *, cc=None, msgid=None, in_reply_to=None):
    """Send one HTML email on a background thread. Returns the Message-ID used (so the
    caller can store it as the thread anchor), or None when SMTP is unconfigured / no
    recipients. Never raises."""
    s = Settings()
    host = (s.SMTP_HOST or "").strip()
    sender = (s.SMTP_EMAIL or "").strip()
    pw = (s.SMTP_APP_PASSWORD or "").strip()
    if not host or not to_addrs:
        logger.info("[sample-mail] skip (host=%s to=%s) subject=%s", bool(host), to_addrs, subject)
        return None
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    mid = msgid or make_msgid(domain="candorfoods.in")
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content("This message requires an HTML-capable mail client.")
    msg.add_alternative(html, subtype="html")
    rcpts = list(dict.fromkeys([*to_addrs, *(cc or [])]))

    def _go():
        try:
            with smtplib.SMTP(host, s.SMTP_PORT, timeout=15) as srv:
                srv.starttls(context=ssl.create_default_context())
                if sender:
                    srv.login(sender, pw)
                srv.send_message(msg, from_addr=sender, to_addrs=rcpts)
            logger.info("[sample-mail] sent subject=%r to=%s", subject, to_addrs)
        except Exception:  # noqa: BLE001 — best-effort; transport failure must not break the request
            logger.exception("[sample-mail] send failed subject=%r", subject)

    threading.Thread(target=_go, daemon=True).start()
    return mid


# Both buttons hit one endpoint: GET /email/npd-action?request_id&status&email.
# Accept carries the reviewer's email for the recipient-match auth; Hold just needs
# the request_id (it redirects to the portal).
def _accept_url(request_id, email: str) -> str:
    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    return f"{base}/api/v1/sample/email/npd-action?request_id={request_id}&status=accept&email={quote(email)}"


def _hold_url(request_id) -> str:
    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    return f"{base}/api/v1/sample/email/npd-action?request_id={request_id}&status=hold"


def _fmt(v, dash: str = "—") -> str:
    """HTML-escaped single-line value; em-dash when blank. All values rendered into
    the email pass through here (free-text fields are user-controlled)."""
    s = "" if v is None else str(v).strip()
    return _html.escape(s) if s else dash


def _detail_table(req: dict) -> str:
    """The request-detail field grid + optional description block — shared verbatim by
    the reviewer mail and the informative inventory mail so the two never drift. Returns
    the inner <table> markup (table-based + inline styles for Gmail/Outlook safety)."""
    exp = req.get("expected_dispatch_date")
    exp = str(exp)[:10] if exp else "TBC"
    wpp, qty = req.get("weight_per_piece"), req.get("quantity")
    fields = [
        ("Company", _fmt(req.get("company_name"))),
        ("Customer", _fmt(req.get("customer_name"))),
        ("Customer contact", _fmt(req.get("customer_contact"))),
        ("Target NPD article", _fmt(req.get("npd_target_name"))),
        ("Pcs", _fmt(req.get("pcs"))),
        ("Weight per piece", f"{_fmt(wpp)} kg" if wpp is not None else "—"),
        ("Quantity", f"{_fmt(qty)} kg" if qty is not None else "—"),
        ("Warehouse", _fmt(req.get("warehouse"))),
        ("Purpose", _fmt(req.get("purpose_tag") or req.get("purpose_note"))),
        ("Mode of transport", _fmt(req.get("mode_of_transport"))),
        ("Expected dispatch", _fmt(exp)),
        ("Requestor", _fmt(req.get("requestor_team"))),
    ]
    rows = "".join(
        '<tr>'
        f'<td style="padding:7px 14px 7px 0;color:#6b7280;font-size:13px;width:42%;vertical-align:top;border-bottom:1px solid #f1f2f4">{label}</td>'
        f'<td style="padding:7px 0;color:#111827;font-size:13px;font-weight:600;border-bottom:1px solid #f1f2f4">{value}</td>'
        '</tr>'
        for label, value in fields
    )
    desc = req.get("description")
    desc_block = (
        '<tr><td colspan="2" style="padding-top:14px">'
        '<div style="font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.07em;margin-bottom:5px">Description</div>'
        '<div style="font-size:13px;color:#374151;line-height:1.55;background:#f9fafb;border:1px solid #eef0f3;'
        f'border-radius:6px;padding:10px 12px">{_fmt(desc)}</div>'
        '</td></tr>'
    ) if (desc and str(desc).strip()) else ""
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}{desc_block}</table>'


def _review_html(req: dict, reviewer_email: str) -> str:
    """Polished, email-client-safe HTML review notice — mirrors the WhatsApp review
    template (full request detail) + Accept / Hold action buttons. The Accept link
    embeds this reviewer's email for the recipient-match auth check."""
    rid = req.get("request_id")
    accept, hold = _accept_url(rid, reviewer_email), _hold_url(rid)
    type_label = "Customer trial" if req.get("sample_type") == "TRIAL" else "NPD"

    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#f4f5f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px">
 <tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif">
   <tr><td style="background:#ec7211;padding:18px 24px">
     <div style="color:#ffffff;font-size:12px;opacity:.92;letter-spacing:.05em">NEW {type_label.upper()} SAMPLE REQUEST</div>
     <div style="color:#ffffff;font-size:24px;font-weight:700;margin-top:3px">{rid}</div>
   </td></tr>
   <tr><td style="padding:22px 24px 4px">
     <p style="margin:0 0 16px;font-size:14px;color:#374151;line-height:1.5">A new {type_label} sample request needs your review. Tap <b>Accept</b> to approve, or <b>Hold</b> to open it on the portal and record a reason.</p>
     {_detail_table(req)}
   </td></tr>
   <tr><td style="padding:18px 24px 26px">
     <table role="presentation" cellpadding="0" cellspacing="0"><tr>
       <td style="padding-right:12px">
         <a href="{accept}" style="display:inline-block;background:#16a34a;color:#ffffff;font-size:14px;font-weight:600;text-decoration:none;padding:12px 30px;border-radius:6px">&#10003;&nbsp; Accept</a>
       </td>
       <td>
         <a href="{hold}" style="display:inline-block;background:#f59e0b;color:#ffffff;font-size:14px;font-weight:600;text-decoration:none;padding:12px 30px;border-radius:6px">&#9208;&nbsp; Hold</a>
       </td>
     </tr></table>
   </td></tr>
   <tr><td style="padding:14px 24px;background:#f9fafb;border-top:1px solid #eef0f3">
     <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.5">You're receiving this as an NPD reviewer. &ldquo;Accept&rdquo; approves the request; &ldquo;Hold&rdquo; opens it on the portal to capture a reason.</p>
   </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


# event -> (header colour, header eyebrow, intro line, status-pill label/colour).
# Drives the informative inventory mail; "created" is the neutral logged notice, the
# others mirror the NPD outcome.
_INV_EVENT = {
    "created":  ("#ec7211", "SAMPLE REQUEST LOGGED",   "A new sample request has been logged in the ERP. This notice is for your visibility — no action is required from inventory at this stage.", "Logged",   "#ec7211"),
    "accepted": ("#16a34a", "SAMPLE REQUEST ACCEPTED", "The NPD team has accepted this sample request. Sharing the details for your visibility.", "Accepted", "#16a34a"),
    "on hold":  ("#f59e0b", "SAMPLE REQUEST ON HOLD",  "The NPD team has placed this sample request on hold. Sharing the details for your visibility.", "On hold", "#b45309"),
}


def _informative_html(req: dict, event: str) -> str:
    """Polished, email-client-safe HTML notice for inventory_manager — same detail card
    as the reviewer mail but with NO action buttons (informational only). The header
    colour, eyebrow and status pill reflect the event (created / accepted / on hold)."""
    rid = req.get("request_id")
    hdr, eyebrow, intro, pill, pill_bg = _INV_EVENT.get(
        event, ("#6b7280", f"SAMPLE REQUEST {(event or '').upper()}",
                "Sharing the details of this sample request for your visibility.",
                _fmt(event).upper() if event else "Update", "#6b7280"))

    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#f4f5f7">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 12px">
 <tr><td align="center">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif">
   <tr><td style="background:{hdr};padding:18px 24px">
     <div style="color:#ffffff;font-size:12px;opacity:.92;letter-spacing:.05em">{eyebrow}</div>
     <div style="color:#ffffff;font-size:24px;font-weight:700;margin-top:3px">{rid}</div>
   </td></tr>
   <tr><td style="padding:22px 24px 4px">
     <div style="margin:0 0 14px">
       <span style="display:inline-block;background:{pill_bg};color:#ffffff;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:4px 12px;border-radius:999px">{pill}</span>
     </div>
     <p style="margin:0 0 16px;font-size:14px;color:#374151;line-height:1.5">{intro}</p>
     {_detail_table(req)}
   </td></tr>
   <tr><td style="padding:14px 24px 24px;background:#f9fafb;border-top:1px solid #eef0f3">
     <p style="margin:0;font-size:11px;color:#9ca3af;line-height:1.5">You're receiving this as an inventory manager, for visibility. This is an informational notice — no action is required from you here.</p>
   </td></tr>
  </table>
 </td></tr>
</table>
</body></html>"""


def _anchor_msgid(request_id, email: str) -> str:
    """Deterministic Message-ID per (request, recipient). Because each reviewer gets
    their OWN message (the Accept link embeds their email), a single stored anchor
    only threaded the first reviewer's mailbox. Deriving the anchor from
    (request_id, email) instead lets every later mail (reminder / outcome reply /
    informative) reply into the right thread in EACH recipient's mailbox — with no
    per-recipient anchor storage. Stable across sends; RFC-safe local part."""
    h = hashlib.sha1(f"{request_id}:{(email or '').strip().lower()}".encode()).hexdigest()[:16]
    return f"<npd-req-{request_id}-{h}@candorfoods.in>"


async def notify_npd_review_email(conn, req: dict, *, threaded: bool = False) -> None:
    """Email npd_team the request with Accept/Hold buttons.

    threaded=False (initial submit): each reviewer's message is ROOTED at the
    deterministic per-recipient Message-ID, so reminders / outcome replies thread under
    it in THEIR mailbox.

    threaded=True (re-prompt after a HOLD): the SAME buttoned card is sent again as a
    REPLY into that existing thread (In-Reply-To the anchor, fresh Message-ID) — no new
    mail trail. This is the hold→re-offer loop: the reviewer can Accept to end it or Hold
    again to be re-prompted. It's human-driven (one re-send per recorded hold), so there
    is no runaway loop. Best-effort, never raises."""
    rid = req.get("request_id")
    recips = await _emails_for_role(conn, "npd_team")
    if not recips:
        logger.warning("[sample-mail] no npd_team emails — skipping review email for req %s", req.get("id"))
        return
    for em in recips:
        anchor = _anchor_msgid(rid, em)
        if threaded:
            _send(f"Re: NPD sample request {rid} — on hold, still needs a decision",
                  _review_html(req, em), [em], in_reply_to=anchor)
        else:
            _send(f"NPD sample request {rid} — action needed",
                  _review_html(req, em), [em], msgid=anchor)


_INV_SUBJECT = {
    "created": "logged",
    "accepted": "accepted by NPD",
    "on hold": "placed on hold by NPD",
}


async def notify_inventory_informative(conn, req: dict, *, event: str) -> None:
    """Informative (no-button) mail to each inventory_manager on create/accept/hold —
    the polished detail card (see _informative_html), buttons deliberately omitted. The
    'created' mail roots a per-recipient thread; later events reply into it."""
    rid = req.get("request_id")
    recips = await _emails_for_role(conn, "inventory_manager")
    if not recips:
        return
    html = _informative_html(req, event)
    subject = f"Sample request {rid} — {_INV_SUBJECT.get(event, event)}"
    is_root = event == "created"
    for em in recips:
        anchor = _anchor_msgid(rid, em)
        _send(subject, html, [em],
              msgid=anchor if is_root else None,
              in_reply_to=None if is_root else anchor)


async def notify_requestor_email(conn, req: dict, *, action: str, reason: str | None = None) -> None:
    """Closing reply on each reviewer's thread when accepted/held — 'the request is
    updated'. Best-effort."""
    rid = req.get("request_id")
    recips = await _emails_for_role(conn, "npd_team")
    if not recips:
        return
    verb = "ACCEPTED" if (action or "").upper() in ("ACCEPT", "APPROVE") else "ON HOLD"
    reason_html = f"<p>Reason: {_html.escape(str(reason))}</p>" if reason else ""
    html = (f"<div style='font-family:Arial,sans-serif'><h3>Sample request {rid} {verb}</h3>"
            + reason_html + "</div>")
    for em in recips:
        _send(f"Sample request {rid} {verb}", html, [em], in_reply_to=_anchor_msgid(rid, em))


async def notify_inventory_promote_requested(conn, *, dev_jc_id, requestor_uid=None) -> None:
    """Informative mail to inventory_manager when a dev-JC promote is requested."""
    recips = await _emails_for_role(conn, "inventory_manager")
    if not recips:
        return
    html = (f"<div style='font-family:Arial,sans-serif'><h3>Dev job card {dev_jc_id}: "
            f"promote awaiting your acceptance</h3></div>")
    _send(f"Dev job card {dev_jc_id} — promote approval needed", html, recips)


async def send_due_reminders(conn) -> int:
    """Reminder reply on the thread for NPD/TRIAL requests still SUBMITTED/ON_HOLD, capped
    at REMINDER_MAX and no sooner than REMINDER_MIN_HOURS apart. Returns # requests nudged.
    Meant to be run on a cron (the cadence guards make frequent calls safe no-ops).

    A transaction-scoped advisory lock makes two concurrent sweeps mutually
    exclusive: a second caller that can't grab the lock returns 0 immediately,
    so the reminder_count never gets double-incremented by overlapping runs."""
    async with conn.transaction():
        got = await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", 8472013)
        if not got:
            return 0
        rows = await conn.fetch(
            """SELECT *
                 FROM sample_requisitions
                WHERE deleted_at IS NULL AND sample_type IN ('NPD','TRIAL')
                  AND status IN ('SUBMITTED','ON_HOLD')
                  AND reminder_count < $1
                  AND (last_reminder_at IS NULL OR last_reminder_at < NOW() - make_interval(hours => $2))""",
            REMINDER_MAX, REMINDER_MIN_HOURS)
        recips = await _emails_for_role(conn, "npd_team")
        if not recips:
            return 0
        sent = 0
        for r in rows:
            for em in recips:
                _send(f"Reminder: NPD sample request {r['request_id']} still needs a decision",
                      _review_html(dict(r), em), [em], in_reply_to=_anchor_msgid(r["request_id"], em))
            await conn.execute(
                "UPDATE sample_requisitions SET reminder_count = reminder_count + 1, last_reminder_at = NOW() WHERE id = $1",
                r["id"])
            sent += 1
        return sent
