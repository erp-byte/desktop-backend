"""A store user's Accept / Hold tap on the floor_requisition_raised_store WhatsApp message.

notify_service sends the template with quick-reply payloads
"floor_req:accept:<requisition_id>" and "floor_req:hold:<requisition_id>". The shared
webhook (sample/services/whatsapp_service.handle_inbound) calls handle_store_tap
FIRST, before any other flow: the buttons read "Accept" and "Hold", which are also
the NPD review verbs, so an unclaimed tap from a store user who is also an NPD
reviewer would otherwise be read as a decision on a sample request.

Contract with the webhook: return None when the tap is not ours (no payload, or a
payload of another shape) so the other flows run unchanged; once the payload is
ours, OWN the tap — always reply, never fall through, never raise.

What a tap does, while the request is still 'raised':
  Accept -> store_response 'accepted' ("taken up by store")
  Hold   -> store_response 'on_hold'  ("cannot be supplied right now")
with store_response_by / store_response_at (migration 112). The latest tap wins. The
request's status does not change: it stays 'raised' until store issues it.

Only the people who were sent the message may answer it: an active store_head
(notify_service.store_role_holders) whose phone matches the tapping number and whose
granted places cover the request.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.modules.auth.services.phone import normalize as normalize_phone
from app.modules.floor_requisition.services import notify_service

logger = logging.getLogger(__name__)

_PAYLOAD_RE = re.compile(rf"^{notify_service.PAYLOAD_PREFIX}:({notify_service.ACCEPT}|{notify_service.HOLD}):(\d{{1,18}})$")
RESPONSE_FOR = {notify_service.ACCEPT: "accepted", notify_service.HOLD: "on_hold"}
STORES_SCREEN = "Stores > Production Indents"

_HAS_COLUMNS_SQL = """
    SELECT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'floor_requisition' AND column_name = 'store_response')
"""

# Meta may deliver a webhook more than once, or out of order. The tap's own send time
# (the message's Unix `timestamp`) decides which reply is the latest: a delivery that
# is not newer than the recorded one changes nothing and gets no second reply. A same-
# second tap of the OTHER button still counts. Without a timestamp, now() is used.
_RECORD_SQL = """
    UPDATE floor_requisition
       SET store_response = $1, store_response_by = $2,
           store_response_at = COALESCE(to_timestamp($4::bigint), now())
     WHERE requisition_id = $3 AND status = 'raised'
       AND (store_response_at IS NULL
            OR store_response_at < COALESCE(to_timestamp($4::bigint), now())
            OR (store_response_at = to_timestamp($4::bigint) AND store_response IS DISTINCT FROM $1))
    RETURNING requisition_id
"""


def parse_payload(payload: Optional[str]) -> Optional[tuple[str, int]]:
    """('accept' | 'hold', requisition_id) for one of our buttons, else None."""
    m = _PAYLOAD_RE.match((payload or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def _same_phone(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = normalize_phone(a), normalize_phone(b)
    return bool(na) and na == nb


async def _reply(wa: str, text: str) -> None:
    from app.modules.sample.services.whatsapp_service import _send_text  # lazy: avoid an import cycle
    try:
        await _send_text(wa, text)
    except Exception:  # noqa: BLE001 - a failed reply must not undo a recorded response
        logger.exception("[floor-req-wa] reply to %s failed", wa)


def _unix_seconds(value: Any) -> Optional[int]:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else None


async def handle_store_tap(conn, wa: str, payload: Optional[str],
                           sent_at: Any = None) -> Optional[dict[str, Any]]:
    """`sent_at` is the inbound message's Meta `timestamp` (Unix seconds, as text)."""
    parsed = parse_payload(payload)
    if parsed is None:
        return None
    action, rid = parsed
    result: dict[str, Any] = {"ok": False, "flow": "floor_requisition", "requisition_id": rid, "action": action}
    try:
        # Who is asking comes first, so a number that is not a store user learns
        # nothing about which request numbers exist.
        holders = await notify_service.store_role_holders(conn)
        mine = [h for h in holders if _same_phone(h["phone"], wa)]
        if not mine:
            await _reply(wa, "This number is not registered to a store user, so your reply was not recorded.")
            return {**result, "reason": "not_a_store_user"}

        req = await conn.fetchrow(
            "SELECT requisition_id, status, material_sku_name, warehouse, floor "
            "FROM floor_requisition WHERE requisition_id = $1", rid)
        if req is None:
            await _reply(wa, f"Request {rid} was not found, so your reply was not recorded.")
            return {**result, "reason": "not_found"}
        place = f"{req['warehouse']} · {req['floor']}"

        allowed = [h for h in mine if notify_service.covers_place(
            h["allowed_warehouses"], h["allowed_floors"], req["warehouse"], req["floor"])]
        if not allowed:
            await _reply(wa, f"You are not assigned to {place}, so your reply to request {rid} was not recorded.")
            return {**result, "reason": "place_not_allowed"}
        user = allowed[0]
        actor = user["name"] or user["email"] or user["phone"] or f"user:{user['user_id']}"

        if req["status"] != "raised":
            await _reply(wa, f"Request {rid} is already {req['status']}, so there is nothing to change.")
            return {**result, "ok": True, "reason": "already_" + str(req["status"])}

        if not await conn.fetchval(_HAS_COLUMNS_SQL):
            logger.warning("[floor-req-wa] #%s %s tap not recorded: migration 112 (store_response) is not applied",
                           rid, action)
            await _reply(wa, f"Your reply to request {rid} could not be recorded yet. "
                             f"Please use {STORES_SCREEN} instead.")
            return {**result, "reason": "store_response_not_installed"}

        response = RESPONSE_FOR[action]
        updated = await conn.fetchval(_RECORD_SQL, response, actor, rid, _unix_seconds(sent_at))
        if updated is None:
            status = await conn.fetchval("SELECT status FROM floor_requisition WHERE requisition_id = $1", rid)
            if status == "raised":
                # A redelivered or older tap: a newer reply is already recorded. Say nothing.
                logger.info("[floor-req-wa] #%s %s tap ignored: stale or duplicate delivery (sent_at=%s)",
                            rid, action, sent_at)
                return {**result, "ok": True, "reason": "stale_or_duplicate"}
            await _reply(wa, f"Request {rid} is already {status}, so there is nothing to change.")
            return {**result, "ok": True, "reason": f"already_{status}"}

        article = req["material_sku_name"]
        if response == "accepted":
            await _reply(wa, f"Noted. Request {rid} ({article}) is taken up by {actor}. "
                             f"Issue it in {STORES_SCREEN} once the material is sent to {place}.")
        else:
            await _reply(wa, f"Noted. Request {rid} ({article}) is on hold. The floor can see this on the job card. "
                             "Tap Accept on the same message when you can supply it.")
        logger.info("[floor-req-wa] #%s %s by %s", rid, response, actor)
        return {**result, "ok": True, "store_response": response, "by": actor}
    except Exception:  # noqa: BLE001 - own the tap: never fall through, never raise
        logger.exception("[floor-req-wa] #%s %s tap failed", rid, action)
        await _reply(wa, f"Sorry, your reply to request {rid} could not be recorded. "
                         f"Please use {STORES_SCREEN} instead.")
        return {**result, "reason": "error"}
