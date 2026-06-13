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


def _accept_url(request_id, email: str) -> str:
    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    return f"{base}/api/v1/sample/email/npd-accept?request_id={request_id}&email={quote(email)}&status=accept"


def _hold_url(request_id) -> str:
    base = Settings().PUBLIC_BACKEND_URL.rstrip("/")
    return f"{base}/api/v1/sample/email/npd-hold?request_id={request_id}"


def _button_html(req: dict, reviewer_email: str) -> str:
    rid = req.get("request_id")
    a, h = _accept_url(rid, reviewer_email), _hold_url(rid)
    # rid is an int (safe); the free-text fields are user-controlled → HTML-escape.
    target = _html.escape(str(req.get("npd_target_name") or "—"))
    qty = _html.escape(str(req.get("quantity") or "—"))
    requestor = _html.escape(str(req.get("requestor_team") or "—"))
    return f"""<div style="font-family:Arial,sans-serif">
      <h2>NPD sample request {rid}</h2>
      <p>Target: {target} &middot; Qty: {qty} &middot; Requestor: {requestor}</p>
      <p><a href="{a}" style="background:#16a34a;color:#fff;padding:10px 18px;border-radius:4px;text-decoration:none">&#10003; Accept</a>
         &nbsp;<a href="{h}" style="background:#f59e0b;color:#fff;padding:10px 18px;border-radius:4px;text-decoration:none">&#9208; Hold</a></p>
    </div>"""


def _anchor_msgid(request_id, email: str) -> str:
    """Deterministic Message-ID per (request, recipient). Because each reviewer gets
    their OWN message (the Accept link embeds their email), a single stored anchor
    only threaded the first reviewer's mailbox. Deriving the anchor from
    (request_id, email) instead lets every later mail (reminder / outcome reply /
    informative) reply into the right thread in EACH recipient's mailbox — with no
    per-recipient anchor storage. Stable across sends; RFC-safe local part."""
    h = hashlib.sha1(f"{request_id}:{(email or '').strip().lower()}".encode()).hexdigest()[:16]
    return f"<npd-req-{request_id}-{h}@candorfoods.in>"


async def notify_npd_review_email(conn, req: dict) -> None:
    """On NPD/TRIAL submit — email npd_team the request with Accept/Hold buttons. Each
    reviewer's message is rooted at a deterministic per-recipient Message-ID so the
    reminders + outcome reply thread under it in THEIR mailbox. Best-effort, never raises."""
    rid = req.get("request_id")
    recips = await _emails_for_role(conn, "npd_team")
    if not recips:
        logger.warning("[sample-mail] no npd_team emails — skipping review email for req %s", req.get("id"))
        return
    for em in recips:
        _send(f"NPD sample request {rid} — action needed",
              _button_html(req, em), [em], msgid=_anchor_msgid(rid, em))


async def notify_inventory_informative(conn, req: dict, *, event: str) -> None:
    """Informative (no-button) mail to each inventory_manager on create/accept/hold. The
    'created' mail roots a per-recipient thread; later events reply into it."""
    rid = req.get("request_id")
    recips = await _emails_for_role(conn, "inventory_manager")
    if not recips:
        return
    target = _html.escape(str(req.get("npd_target_name") or "—"))
    html = (f"<div style='font-family:Arial,sans-serif'><h3>Sample request {rid} — {event}</h3>"
            f"<p>Target: {target}</p></div>")
    is_root = event == "created"
    for em in recips:
        anchor = _anchor_msgid(rid, em)
        _send(f"Sample request {rid} — {event}", html, [em],
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
            """SELECT id, request_id, npd_target_name, quantity, requestor_team
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
                      _button_html(dict(r), em), [em], in_reply_to=_anchor_msgid(r["request_id"], em))
            await conn.execute(
                "UPDATE sample_requisitions SET reminder_count = reminder_count + 1, last_reminder_at = NOW() WHERE id = $1",
                r["id"])
            sent += 1
        return sent
