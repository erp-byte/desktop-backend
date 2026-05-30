"""Lightweight SMTP notification helper for the production module.

Best-effort: failures are logged and swallowed so a transient mail outage never
fails the API request that triggered the notification.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings

logger = logging.getLogger(__name__)

# Business-head registry: key -> (display name, email)
BUSINESS_HEADS: dict[str, tuple[str, str]] = {
    "prashant_pal": ("Prashant Pal", "prashant.pal@candorfoods.in"),
    "ajay_bajaj":   ("Ajay Bajaj",   "ajay@candorfoods.in"),
    "rakesh_ratra": ("Rakesh Ratra", "rakesh@candorfoods.in"),
    "yash_gawdi":   ("Yash Gawdi",   "yash@candorfoods.in"),
}

# Always CC'd on RTV-disposition notifications
CONSTANT_CC: list[str] = [
    "sunil.jasoria@candorfoods.in",
    "b.hrithik@candorfoods.in",
]


def business_head_email(key: str | None) -> str | None:
    if not key:
        return None
    entry = BUSINESS_HEADS.get(key)
    return entry[1] if entry else None


async def _lookup_user_email(conn, identifier: str | None) -> str | None:
    """Best-effort lookup: treat identifier as phone, user_id, or email."""
    if not identifier:
        return None
    if "@" in identifier:
        return identifier
    row = await conn.fetchrow(
        "SELECT email FROM users WHERE phone = $1 OR CAST(user_id AS TEXT) = $1 LIMIT 1",
        identifier,
    )
    return row["email"] if row and row["email"] else None


def _send(subject: str, body: str, to_addrs: list[str], cc_addrs: list[str]) -> None:
    settings = Settings()
    if not settings.SMTP_HOST:
        logger.info("[mail] SMTP_HOST not configured — skipping send (to=%s cc=%s subject=%s)",
                    to_addrs, cc_addrs, subject)
        return

    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg.set_content(body)

    recipients = list(dict.fromkeys([*to_addrs, *cc_addrs]))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            if settings.SMTP_USE_TLS:
                s.starttls(context=ssl.create_default_context())
            if settings.SMTP_USERNAME:
                s.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            s.send_message(msg, from_addr=settings.SMTP_FROM, to_addrs=recipients)
        logger.info("[mail] sent subject=%r to=%s cc=%s", subject, to_addrs, cc_addrs)
    except Exception:
        logger.exception("[mail] failed subject=%r to=%s cc=%s", subject, to_addrs, cc_addrs)


def _build_cc(business_head_key: str | None) -> list[str]:
    cc = list(CONSTANT_CC)
    bh = business_head_email(business_head_key)
    if bh and bh not in cc:
        cc.insert(0, bh)
    return cc


async def send_rtv_disposition_email(
    conn,
    *,
    rtv_id: str,
    disposition_id: str,
    disposition_type: str,
    decided_by: str,
    qc_remarks: str | None,
    business_head: str | None,
    linked_internal_order: str | None,
    linked_offgrade_lot: str | None,
) -> None:
    cc = _build_cc(business_head)
    to_email = await _lookup_user_email(conn, decided_by)
    if not to_email:
        to_email = cc.pop(0) if cc else None
    if not to_email:
        logger.warning("[mail] no recipient resolvable for RTV %s — skipping", rtv_id)
        return

    bh_label = BUSINESS_HEADS.get(business_head or "", (None, None))[0] or "—"
    subject = f"[RTV] Disposition {disposition_type.upper()} — {rtv_id}"
    body = (
        f"An RTV disposition has been recorded.\n\n"
        f"RTV ID            : {rtv_id}\n"
        f"Disposition ID    : {disposition_id}\n"
        f"Disposition Type  : {disposition_type}\n"
        f"Decided By        : {decided_by}\n"
        f"Business Head     : {bh_label}\n"
        f"QC Remarks        : {qc_remarks or '—'}\n"
        f"Linked Indent     : {linked_internal_order or '—'}\n"
        f"Linked Off-grade  : {linked_offgrade_lot or '—'}\n"
    )
    _send(subject, body, [to_email], cc)


async def _lookup_admin_emails(conn) -> list[str]:
    """Return the email addresses of every active admin user.

    Admin = users.role_id → auth_role with is_admin = TRUE. Inactive users
    are filtered out. NULL / empty emails are dropped. Order is stable
    (email-ascending) so the list reads consistently across calls — handy
    for log review.
    """
    rows = await conn.fetch(
        """
        SELECT u.email
        FROM   users u
        JOIN   auth_role r ON u.role_id = r.role_id
        WHERE  r.is_admin = TRUE
          AND  u.is_active = TRUE
          AND  u.email IS NOT NULL
          AND  TRIM(u.email) <> ''
        ORDER BY u.email
        """,
    )
    return [r["email"] for r in rows]


async def send_plan_deletion_email(
    conn,
    *,
    plan_id: int,
    plan_name: str | None,
    warehouse: str | None,
    entity: str | None,
    reason: str,
    deleted_by: str,
) -> int:
    """Fan out a notification to every active admin when an approved plan
    is deleted. Returns the number of admin recipients the message was
    sent to (or 0 if no admins are configured / SMTP is off).

    Best-effort — failures here are logged inside `_send` and never raise,
    so the API call that triggered the delete still returns success even
    if SMTP is unreachable.
    """
    admins = await _lookup_admin_emails(conn)
    if not admins:
        logger.warning(
            "[mail] plan-delete notification skipped — no active admin emails on file (plan_id=%s)",
            plan_id,
        )
        return 0
    label = plan_name or f"Plan #{plan_id}"
    subject = f"[Plan] APPROVED plan deleted — {label}"
    body = (
        f"An approved production plan has been deleted by an operator.\n\n"
        f"Plan ID         : {plan_id}\n"
        f"Plan Name       : {plan_name or '—'}\n"
        f"Entity          : {entity or '—'}\n"
        f"Warehouse       : {warehouse or '—'}\n"
        f"Deleted By      : {deleted_by or '—'}\n"
        f"Reason          : {reason or '—'}\n\n"
        f"Note: any per-floor job cards auto-generated when the plan was\n"
        f"approved are NOT automatically cancelled. Please verify and cancel\n"
        f"the related job cards individually if appropriate.\n"
    )
    # All admins go in `To:` (everyone is a primary recipient for a
    # destructive admin-audit notification). No CC.
    _send(subject, body, admins, [])
    return len(admins)


async def send_rtv_discard_email(
    conn,
    *,
    rtv_id: str,
    disposition_id: str,
    reason: str,
    authorised_by: str,
    business_head: str | None,
) -> None:
    cc = _build_cc(business_head)
    to_email = await _lookup_user_email(conn, authorised_by)
    if not to_email:
        to_email = cc.pop(0) if cc else None
    if not to_email:
        logger.warning("[mail] no recipient resolvable for RTV discard %s — skipping", rtv_id)
        return

    bh_label = BUSINESS_HEADS.get(business_head or "", (None, None))[0] or "—"
    subject = f"[RTV] Discard APPROVED — {rtv_id}"
    body = (
        f"An RTV discard has been approved and written off.\n\n"
        f"RTV ID            : {rtv_id}\n"
        f"Disposition ID    : {disposition_id}\n"
        f"Authorised By     : {authorised_by}\n"
        f"Business Head     : {bh_label}\n"
        f"Reason            : {reason}\n"
    )
    _send(subject, body, [to_email], cc)
