"""WhatsApp-based password-reset OTP service.

Flow (see auth.router):
    1. POST /password/reset/send-otp  → generate 6-digit OTP, upsert hash into
       auth_password_reset_otp, fire WhatsApp Cloud API template message.
    2. POST /password/reset/verify    → match phone+otp, set the new password,
       erase the OTP row, revoke live refresh tokens.

Why a 6-digit numeric OTP: matches the `visitor_revisit_otp` Authentication
template (configured on the Meta WA Business account) whose body reads
"{{1}} is your verification code." plus a one-time "Copy code" URL button.
Both the body parameter and the URL button payload carry the same OTP string.

Why bcrypt the OTP at rest: prevents a DB read from yielding a usable token
during the 60-second window. Verification cost is negligible at this
volume — single-digit OTPs/minute across the org in normal use.

Why upsert on (user_id): a resend should INVALIDATE the prior OTP. Storing
one row per user enforces that without a separate cleanup step. Successful
verification then DELETEs the row outright.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import httpx

from app.modules.auth.services.phone import normalize as normalize_phone

logger = logging.getLogger(__name__)

# Length matches the Meta-approved Authentication template ({{1}} is 6 digits).
OTP_LENGTH = 6
# 1-minute validity — per ops decision. Matches the user-visible "06:46"
# countdown shown on the template preview, but we stop short at 60s since
# we don't have a resend cooldown beyond "user clicks the button again".
OTP_TTL_SECONDS = 60

# WhatsApp Cloud API template metadata. Defaults match the working
# vms_referrence integration (visitor_revisit_otp · English (US)); override
# via env if the template name / language changes in Meta Business Manager.
TEMPLATE_NAME = os.environ.get("WHATSAPP_OTP_TEMPLATE_NAME", "visitor_revisit_otp")
TEMPLATE_LANG = os.environ.get("WHATSAPP_OTP_LANG", "en_US")
# Graph API host + version. Defaulted to v21.0 to match vms_referrence — the
# working sister integration on the same WABA. v22 also accepts the same
# payload, but pinning to the version VMS has battle-tested means we don't
# have to chase a Graph deprecation alone.
GRAPH_API_BASE = os.environ.get("WHATSAPP_GRAPH_BASE", "https://graph.facebook.com/v21.0")


def _is_truthy(s: str | None) -> bool:
    """Parse a env-var boolean. Accepts 1/true/yes/on (case-insensitive)."""
    return (s or "").strip().lower() in {"1", "true", "yes", "on"}


def _wa_enabled() -> bool:
    """Three-gate enablement check, evaluated per-call so an ops flip of
    WHATSAPP_ENABLED takes effect without a restart:

      1. WHATSAPP_ENABLED must be truthy. Defaults to true so existing
         deployments don't silently break — VMS uses this as an explicit
         on/off lever and we mirror the pattern.
      2. WHATSAPP_ACCESS_TOKEN must be set.
      3. WHATSAPP_PHONE_NUMBER_ID must be set.

    Gate 1 lets ops disable WhatsApp without unsetting the creds (useful
    when Meta is misbehaving and we want to fall back to log-only mode for
    a few minutes). Gates 2-3 catch genuine config gaps.
    """
    if not _is_truthy(os.environ.get("WHATSAPP_ENABLED", "true")):
        return False
    return bool(
        os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
        and os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    )


def generate_otp() -> str:
    """Cryptographically random 6-digit decimal OTP, leading zeros preserved."""
    # secrets.randbelow gives a uniform integer in [0, 10**OTP_LENGTH); zfill
    # keeps leading zeros so "012345" stays a valid 6-digit code.
    return str(secrets.randbelow(10 ** OTP_LENGTH)).zfill(OTP_LENGTH)


def hash_otp(plain: str) -> str:
    # rounds=10 — verifications run once per user-initiated reset, so the
    # extra speed over rounds=12 is welcome without compromising the security
    # bound (60s exposure window).
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_otp(plain: str, stored_hash: str) -> bool:
    if not stored_hash or not plain:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=OTP_TTL_SECONDS)


# ── persistence helpers ──────────────────────────────────────────────────


async def upsert_otp(conn, *, user_id: int, otp_hash: str, expires: datetime, phone: str | None) -> None:
    """Insert or overwrite the OTP row for `user_id`.

    The single-row-per-user PK makes a resend a strict overwrite — there is
    never more than one valid OTP per user at any moment.
    """
    await conn.execute(
        """
        INSERT INTO auth_password_reset_otp (user_id, otp_hash, expires_at, last_phone, created_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (user_id) DO UPDATE
           SET otp_hash   = EXCLUDED.otp_hash,
               expires_at = EXCLUDED.expires_at,
               last_phone = EXCLUDED.last_phone,
               created_at = NOW()
        """,
        user_id, otp_hash, expires, phone,
    )


async def fetch_otp(conn, *, user_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT user_id, otp_hash, expires_at FROM auth_password_reset_otp WHERE user_id = $1",
        user_id,
    )
    return dict(row) if row else None


async def delete_otp(conn, *, user_id: int) -> None:
    await conn.execute("DELETE FROM auth_password_reset_otp WHERE user_id = $1", user_id)


# ── WhatsApp Cloud API send ──────────────────────────────────────────────


def _format_phone_for_wa(raw: str) -> str:
    """WhatsApp expects the recipient as an E.164 string with NO leading '+'.

    Our `normalize_phone` returns something like '+919876543210'; strip the
    leading '+' so the Graph API doesn't reject the number as malformed.
    """
    norm = normalize_phone(raw) or raw
    return norm.lstrip("+")


def _build_template_payload(to_e164: str, otp: str) -> dict[str, Any]:
    """Build the JSON body for the Cloud API send.

    Meta's Authentication templates require BOTH:
      * a body component carrying the OTP as text param {{1}}
      * a button component (sub_type=url, index=0) whose `text` param is
        ALSO the OTP — this is the "Copy code" button's payload.

    Sending the OTP only in the body would render the message correctly but
    leave the Copy code button inert because the server-side template engine
    won't substitute {{1}} in the URL.
    """
    return {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": otp}],
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": otp}],
                },
            ],
        },
    }


async def send_otp_via_whatsapp(phone_raw: str, otp: str) -> dict[str, Any]:
    """Fire the template message. Returns the Graph API response dict on
    success. Raises RuntimeError when WhatsApp is enabled but the Graph call
    fails — the router translates that into a 502 visible to the operator.

    Log-only fallback: if WhatsApp is disabled (`WHATSAPP_ENABLED=false`)
    OR creds are missing, we log the OTP plainly and return a sentinel dict
    so the router can still claim success. Mirrors vms_referrence which
    explicitly toggles delivery via `whatsapp_enabled`.
    """
    if not _wa_enabled():
        reason = (
            "WhatsApp disabled via WHATSAPP_ENABLED"
            if not _is_truthy(os.environ.get("WHATSAPP_ENABLED", "true"))
            else "WhatsApp creds missing (WHATSAPP_ACCESS_TOKEN / "
                 "WHATSAPP_PHONE_NUMBER_ID)"
        )
        logger.warning("%s — falling back to log-only OTP delivery.", reason)
        logger.info("OTP for %s = %s (log-only, NOT sent over WhatsApp)", phone_raw, otp)
        return {"dev_fallback": True, "reason": reason}

    token = os.environ["WHATSAPP_ACCESS_TOKEN"].strip()
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip()

    to = _format_phone_for_wa(phone_raw)
    payload = _build_template_payload(to, otp)
    url = f"{GRAPH_API_BASE}/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            logger.exception("WhatsApp Cloud API request failed for %s", to)
            raise RuntimeError(f"WhatsApp send failed: {e}") from e

    if resp.status_code >= 400:
        logger.error(
            "WhatsApp Cloud API rejected OTP send: status=%s body=%s",
            resp.status_code, resp.text[:500],
        )
        raise RuntimeError(f"WhatsApp send failed: HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}

    # Surface the Meta-side ack on the uvicorn console so an operator can
    # see message id + status without grepping a probe. We log the wamid
    # and status only — never the OTP itself — so this stays safe to keep
    # in production logs.
    try:
        msg = (body.get("messages") or [{}])[0]
        wamid = msg.get("id")
        status = msg.get("message_status")
        contacts = body.get("contacts") or [{}]
        wa_id = contacts[0].get("wa_id") if contacts else None
        logger.info(
            "WhatsApp OTP send accepted: to=%s wa_id=%s status=%s wamid=%s",
            to, wa_id, status, wamid,
        )
    except (AttributeError, IndexError, KeyError, TypeError):
        logger.info("WhatsApp OTP send returned: %s", str(body)[:300])

    return body
