"""Answering a transfer request from its WhatsApp notice.

request_notify sends the store's notice with the quick replies Accept / Hold and the
inventory managers' with Approve / Hold / Reject. Each button's payload names the
decision and the request ('trq:accept:41250001', 'trq:store_hold:...',
'trq:approve:...', 'trq:hold:...', 'trq:reject:...'). The shared webhook
(sample/services/whatsapp_service.handle_inbound) calls handle_reply right after the
floor-requisition taps.

  Accept / Approve  the decision is made at once, as the user the tapping number belongs to.
  Hold / Reject     the bot asks for the reason; the next text message from that number
                    within REASON_MINUTES is the reason, and the hold or reject is made
                    with it (transfer_request_wa_pending, migrations 126 / 127). A message
                    after that is not taken as the reason (it goes to the other flows), and
                    a new tap replaces the hold or reject that was waiting.

The number must belong to an active ERP user, and every decision runs through
request_action.decide with that user, so the ERP's checks apply unchanged: the role
permission, the store's warehouse lock, nobody deciding their own request, the request
still at that step. When the request has moved on - decided in the ERP, or by someone
else - the reply says nothing was done and what state it is in.

A tap without our payload that quotes one of our notices (transfer_request_wa_message,
migration 126) is matched by the quoted message and the button's text.

Contract with the webhook: return None when the message is not ours, so the other flows
run unchanged; once it is ours, own it - always reply, never fall through, never raise.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.sites import canonical_site
from app.modules.transfer.services import request_action
from app.modules.transfer.services.request_notify import PAYLOAD_PREFIX, REQUEST_PAGE

logger = logging.getLogger(__name__)

REASON_MINUTES = 30
# The actions the notices' buttons send (the store's template has no Reject button).
_PAYLOAD_RE = re.compile(rf"^{PAYLOAD_PREFIX}:(accept|store_hold|approve|hold|reject):(\d{{1,18}})$")
# A quoted notice's button, by its text: (stage, first word) -> action.
_BY_TEXT = {("store", "accept"): "accept", ("store", "hold"): "store_hold",
            ("manager", "approve"): "approve", ("manager", "hold"): "hold", ("manager", "reject"): "reject"}
_BUTTONS = {"store": "Accept or Hold", "manager": "Approve, Hold or Reject"}
# What a Hold / Reject tap is doing, for the question that asks for its reason.
_DOING = {"store_hold": "putting {req} on hold", "hold": "putting {req} on hold",
          "store_reject": "rejecting {req}", "reject": "rejecting {req}"}
NOT_REGISTERED = "Sorry, this WhatsApp number is not registered to an ERP user, so nothing was done."


def parse_payload(payload: Optional[str]) -> Optional[tuple[str, int]]:
    """(action, request id) for one of our buttons, else None."""
    m = _PAYLOAD_RE.match((payload or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def _ws():
    from app.modules.sample.services import whatsapp_service  # lazy: its handle_inbound calls this module
    return whatsapp_service


async def _reply(wa: str, text: str) -> Optional[str]:
    """Send a text reply; its wamid. A failed reply is logged, never raised."""
    ws = _ws()
    try:
        return ws._wamid(await ws._send_text(wa, text))
    except Exception:  # noqa: BLE001
        logger.exception("[trq-wa] reply to %s failed", wa)
        return None


def _erp_link(request_id: int) -> str:
    from app.modules.production.services.wa_notify import current_settings
    try:
        base = (current_settings().WEB_APP_URL or "").rstrip("/")
    except Exception:  # noqa: BLE001
        base = ""
    return f"{base}{REQUEST_PAGE.format(id=request_id)}"


def _is_missing_table(exc: Exception) -> bool:
    return type(exc).__name__ == "UndefinedTableError"


async def handle_reply(conn, wa: str, text: str, context_id: Optional[str] = None,
                       raw: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """`wa` is the sender as handle_inbound formats it (E.164 digits, no '+'); `raw` the
    Meta message object. Returns a result dict when the message is ours, else None."""
    payload = _ws()._button_payload(raw) if raw else None
    parsed = parse_payload(payload)
    if parsed is not None:
        return await _tap(conn, wa, *parsed)
    if context_id:
        quoted = await _quoted(conn, context_id)
        if quoted is not None:
            return await _quoted_tap(conn, wa, text, *quoted)
    if raw is not None and raw.get("type") in ("button", "interactive"):
        return None    # another flow's button
    return await _reason(conn, wa, text, context_id)


async def _quoted_tap(conn, wa: str, text: str, request_id: int, stage: str) -> dict[str, Any]:
    """A message quoting one of our notices: its button's text names the decision; any
    other text is the reason when this number is holding or rejecting that request."""
    first = (text or "").strip().split(maxsplit=1)[0].lower() if (text or "").strip() else ""
    action = _BY_TEXT.get((stage, first))
    if action is not None:
        return await _tap(conn, wa, action, request_id)
    answered = await _reason(conn, wa, text, None, quoted_request=request_id)
    if answered is not None:
        return answered
    await _reply(wa, f"Tap {_BUTTONS.get(stage, 'a button')} on the transfer request message to answer it.")
    return {"ok": False, "flow": "transfer_request", "request_id": request_id, "reason": "unparsed"}


async def _quoted(conn, context_id: str) -> Optional[tuple[int, str]]:
    """(request id, stage) of the notice a message quotes, when it is one of ours."""
    try:
        row = await conn.fetchrow(
            "SELECT request_id, stage FROM transfer_request_wa_message WHERE wamid = $1", context_id)
    except Exception as exc:  # noqa: BLE001 - never abort a message that belongs to another flow
        if not _is_missing_table(exc):
            logger.exception("[trq-wa] sent-message lookup failed for ctx=%s", context_id)
        return None
    return (row["request_id"], row["stage"]) if row else None


# ── a tap ────────────────────────────────────────────────────────────────────
async def _tap(conn, wa: str, action: str, request_id: int) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "flow": "transfer_request", "request_id": request_id, "action": action}
    try:
        # Who is asking comes first, so a number that is not an ERP user learns
        # nothing about the request.
        user = await request_action.load_user(conn, phone=wa)
        if user is None:
            await _reply(wa, NOT_REGISTERED)
            return {**result, "reason": "unknown_number"}

        if request_action.STEPS[action].needs_reason:
            refusal, req = await request_action.precheck(conn, user, request_id, action)
            if refusal is not None:
                await _reply(wa, refusal["message"])
                return {**result, "reason": refusal["kind"]}
            if not await _arm(conn, wa, request_id, action, user.user_id):
                await _reply(wa, "Holding or rejecting from WhatsApp is not available yet, so nothing was done. "
                                 f"Please do it in the ERP: {_erp_link(request_id)}")
                return {**result, "reason": "reason_capture_not_installed"}
            no = (req.get("request_no") or "").strip() or f"#{request_id}"
            route = f"{canonical_site(req.get('from_warehouse')) or '-'} to {canonical_site(req.get('to_warehouse')) or '-'}"
            doing = _DOING[action].format(req=f"transfer request {no} ({route})")
            wamid = await _reply(wa, f"You are {doing}. Please type the reason and send it within "
                                     f"{REASON_MINUTES} minutes. Nothing changes until you do.")
            if wamid:
                await _set_prompt(conn, wa, wamid)
            return {**result, "ok": True, "awaiting": "reason"}

        await _disarm(conn, wa)    # a new decision replaces a hold / reject still waiting for its reason
        outcome = await request_action.decide(conn, user, request_id, action)
        await _reply(wa, outcome["message"])
        return {**result, "ok": outcome["ok"], "reason": outcome["kind"], "status": outcome["status"]}
    except Exception:  # noqa: BLE001 - own the tap: never fall through, never raise
        logger.exception("[trq-wa] request %s %s tap failed", request_id, action)
        await _reply(wa, f"Sorry, your answer could not be recorded, so nothing was done. "
                         f"Please use the ERP instead: {_erp_link(request_id)}")
        return {**result, "reason": "error"}


# ── the reason for a hold or a reject ────────────────────────────────────────
async def _arm(conn, wa: str, request_id: int, action: str, user_id: int) -> bool:
    """'The next text from this number is the reason' (for a hold or a reject). False when
    the table is missing."""
    try:
        await conn.execute(
            """
            INSERT INTO transfer_request_wa_pending (wa_phone, request_id, action, user_id, prompt_wamid, created_at)
            VALUES ($1, $2, $3, $4, NULL, now())
            ON CONFLICT (wa_phone) DO UPDATE
               SET request_id = EXCLUDED.request_id, action = EXCLUDED.action, user_id = EXCLUDED.user_id,
                   prompt_wamid = NULL, created_at = now()
            """,
            wa, request_id, action, user_id)
        return True
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_table(exc):
            raise
        logger.warning("[trq-wa] transfer_request_wa_pending is missing (migration 126 not applied)")
        return False


async def _set_prompt(conn, wa: str, wamid: str) -> None:
    """Remember the prompt, so a reply that quotes it is the reason too. Best-effort."""
    try:
        await conn.execute("UPDATE transfer_request_wa_pending SET prompt_wamid = $2 WHERE wa_phone = $1", wa, wamid)
    except Exception:  # noqa: BLE001
        logger.exception("[trq-wa] could not store the prompt id for %s", wa)


async def _disarm(conn, wa: str) -> None:
    try:
        await conn.execute("DELETE FROM transfer_request_wa_pending WHERE wa_phone = $1", wa)
    except Exception as exc:  # noqa: BLE001 - nothing was waiting if the table is missing
        if not _is_missing_table(exc):
            logger.exception("[trq-wa] could not clear the waiting hold / reject for %s", wa)


def _expired(created_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - created_at > timedelta(minutes=REASON_MINUTES)


async def _reason(conn, wa: str, text: str, context_id: Optional[str],
                  quoted_request: Optional[int] = None) -> Optional[dict[str, Any]]:
    """A message that is not a tap: the reason, when this number was asked for one. A
    message that quotes something is the reason only when it quotes the prompt, or the
    notice of the request being held or rejected (`quoted_request`)."""
    try:
        row = await conn.fetchrow(
            "SELECT request_id, action, prompt_wamid, created_at FROM transfer_request_wa_pending "
            "WHERE wa_phone = $1", wa)
    except Exception as exc:  # noqa: BLE001 - let the other flows run
        if not _is_missing_table(exc):
            logger.exception("[trq-wa] waiting hold / reject lookup failed for %s", wa)
        return None
    if row is None:
        return None
    if quoted_request is not None and quoted_request != row["request_id"]:
        return None    # it quotes the notice of another request
    if context_id and context_id != row["prompt_wamid"]:
        return None    # it answers some other message
    if _expired(row["created_at"]):
        await _disarm(conn, wa)
        logger.info("[trq-wa] %s of request %s by %s timed out after %d minutes; message left to the other flows",
                    row["action"], row["request_id"], wa, REASON_MINUTES)
        return None

    request_id, action = row["request_id"], row["action"]
    result: dict[str, Any] = {"ok": False, "flow": "transfer_request", "request_id": request_id, "action": action}
    try:
        reason = (text or "").strip()
        if not reason:
            await _reply(wa, "Please type the reason as a text message.")
            return {**result, "reason": "empty_reason"}
        # Take the waiting hold / reject (once: a redelivered message finds it gone).
        taken = await conn.fetchval(
            "DELETE FROM transfer_request_wa_pending WHERE wa_phone = $1 AND request_id = $2 RETURNING request_id",
            wa, request_id)
        if taken is None:
            return {**result, "ok": True, "reason": "duplicate"}
        user = await request_action.load_user(conn, phone=wa)
        if user is None:
            await _reply(wa, NOT_REGISTERED)
            return {**result, "reason": "unknown_number"}
        outcome = await request_action.decide(conn, user, request_id, action, reason)
        await _reply(wa, outcome["message"])
        return {**result, "ok": outcome["ok"], "reason": outcome["kind"], "status": outcome["status"]}
    except Exception:  # noqa: BLE001 - own it: never fall through, never raise
        logger.exception("[trq-wa] request %s %s reason failed", request_id, action)
        await _reply(wa, f"Sorry, that could not be recorded, so nothing was done. "
                         f"Please use the ERP instead: {_erp_link(request_id)}")
        return {**result, "reason": "error"}
