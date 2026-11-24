"""Tell the inventory managers when a floor reports "checked - no movement", and
take their Approve / Reject taps on that WhatsApp notice.

WHO. Every user who can sign in with the inventory_manager role (the shared
recipient rule in production/services/wa_notify.py) and whose warehouses cover the
floor's warehouse. Floors are not matched: an inventory manager answers for the
whole warehouse, and the 17 floors on an inventory manager's profile are not the
full list. The person who reported the check is never told about their own check.

WHAT. One message each, using the Meta UTILITY template no_mov_inv_approval
(English "en", positional; template id 1642681437634143, submitted for review on
23 Nov 2026). Until Meta approves it every send is refused, which is logged and
does nothing else: the check is saved and shows on the screen either way.

    header   "Daily floor check - no movement"            (no variables)
    body     Hello, a floor has reported no stock movement today and needs your approval.

             Floor: {{1}}
             Warehouse: {{2}}
             Date: {{3}}
             Checked by: {{4}} at {{5}}
             Last stock entry on this floor: {{6}}
             Note: {{7}}

             Tap Approve or Reject below, or decide in the ERP under Stock Take > Daily floor checks.
    footer   "Candor Foods - Stock Take"
    buttons  quick replies "Approve" (index 0) and "Reject" (index 1)

Each button is sent with the payload "stfc:<approve|reject>:<check_id>", so a tap
names its check without a lookup table.

THE TAP. handle_tap is called by the shared webhook
(sample/services/whatsapp_service.handle_inbound) next to the transfer-request
taps. It returns None for anything that is not one of these buttons, so the other
flows run unchanged. Once the payload is ours it owns the tap: it always replies,
never falls through, never raises. The number must belong to an active ERP user
who holds stock_take / approve and covers the warehouse; the decision then goes
through floor_check_service.decide, so the ERP's rules apply unchanged (only a
pending check, nobody deciding their own). A Reject from WhatsApp records
"Rejected on WhatsApp" as the reason.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Optional

from app.modules.auth.services.phone import normalize as normalize_phone
from app.modules.production.services.wa_notify import (
    covers_place, current_settings as _settings, param as _param, role_holders,
    send_template as _send_template, whatsapp_off_reason as _whatsapp_off_reason,
)
from app.modules.stock_take.services import floor_check_service as fcs

logger = logging.getLogger(__name__)

APPROVER_ROLES = ("inventory_manager",)
TEMPLATE_NAME = "no_mov_inv_approval"
TEMPLATE_LANG = "en"
PAYLOAD_PREFIX = "stfc"
APPROVE, REJECT = "approve", "reject"              # button index 0, 1
SCREEN = "Stock Take > Daily floor checks"
WA_REJECT_REASON = "Rejected on WhatsApp"
NOT_REGISTERED = "Sorry, this WhatsApp number is not registered to an ERP user, so nothing was done."

_PAYLOAD_RE = re.compile(rf"^{PAYLOAD_PREFIX}:({APPROVE}|{REJECT}):(\d{{1,18}})$")

# The body's fixed text is about 300 characters; Meta refuses a filled-in body
# over 1024, so the seven variables share well under 700.
_BODY_BUDGET = (("floor", 60), ("warehouse", 40), ("day", 30), ("checked_by", 60),
                ("checked_at", 20), ("last_entry", 90), ("note", 200))


# ── what ─────────────────────────────────────────────────────────────────────

def _day(iso: Optional[str]) -> str:
    try:
        return date.fromisoformat(str(iso)).strftime("%a %d %b %Y")
    except (TypeError, ValueError):
        return str(iso or "-")


def _time(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(str(iso)).astimezone(fcs.IST).strftime("%H:%M")
    except (TypeError, ValueError):
        return "-"


def _last_entry(last: Optional[dict[str, Any]]) -> str:
    if not last or not last.get("last_at"):
        return "none recorded"
    at = last["last_at"]
    when = at.astimezone(fcs.IST).strftime("%d %b %Y, %H:%M") if isinstance(at, datetime) else str(at)
    return f"{when} by {last.get('last_by') or 'unknown'}"


def body_params(check: dict[str, Any], last: Optional[dict[str, Any]]) -> list[str]:
    """The seven positional body variables, in the template's order."""
    values = {
        "floor": check.get("floor_label") or check.get("floor_name"),
        "warehouse": check.get("warehouse"),
        "day": _day(check.get("check_day")),
        "checked_by": check.get("checked_by"),
        "checked_at": _time(check.get("checked_at")),
        "last_entry": _last_entry(last),
        "note": check.get("note") or "-",
    }
    return [_param(values[key], limit) for key, limit in _BODY_BUDGET]


def button_payload(action: str, check_id: Any) -> str:
    """What a quick-reply tap sends back: 'stfc:approve:1234'."""
    return f"{PAYLOAD_PREFIX}:{action}:{check_id}"


def template_message(to: str, check: dict[str, Any], last: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The Cloud API request body for one recipient."""
    cid = check.get("check_id")
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": TEMPLATE_NAME,
            "language": {"code": TEMPLATE_LANG},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params(check, last)]},
                {"type": "button", "sub_type": "quick_reply", "index": "0",
                 "parameters": [{"type": "payload", "payload": button_payload(APPROVE, cid)}]},
                {"type": "button", "sub_type": "quick_reply", "index": "1",
                 "parameters": [{"type": "payload", "payload": button_payload(REJECT, cid)}]},
            ],
        },
    }


# ── who ──────────────────────────────────────────────────────────────────────

def approvers_for(holders: list[dict[str, Any]], warehouse: str,
                  exclude_user_id: Optional[int] = None) -> list[dict[str, Any]]:
    """The holders whose warehouses cover this one (floors not matched), less the reporter."""
    return [h for h in holders
            if h["user_id"] != exclude_user_id and covers_place(h["allowed_warehouses"], [], warehouse, "")]


# ── send ─────────────────────────────────────────────────────────────────────

async def notify_approvers(pool, check_id: int) -> dict[str, Any]:
    """WhatsApp the inventory managers about a just-committed check. Never raises."""
    report: dict[str, Any] = {"check_id": check_id, "recipients": [],
                              "whatsapp": {"sent": 0, "failed": [], "skipped": None}, "error": None}
    try:
        async with pool.acquire() as conn:
            check = await fcs.get_check(conn, check_id)
            if check is None:
                report["error"] = "check_not_found"
                return report
            last = await fcs.last_entry(conn, check["warehouse"], check["floor_name"],
                                        date.fromisoformat(check["check_day"]))
            holders = await role_holders(conn, APPROVER_ROLES)
        people = approvers_for(holders, check["warehouse"], check.get("checked_by_user_id"))
        report["recipients"] = [p["name"] for p in people]
        if not people:
            report["whatsapp"]["skipped"] = "no_inventory_manager_for_this_warehouse"
            logger.warning("[floor-check] #%s: no active inventory_manager covers %s - nobody notified",
                           check_id, check["warehouse"])
            return report
        settings = _settings()
        off = _whatsapp_off_reason(settings)
        if off:
            report["whatsapp"]["skipped"] = off
            return report
        phones = list(dict.fromkeys(
            (normalize_phone(p["phone"]) or p["phone"]).lstrip("+") for p in people if p["phone"]))
        if not phones:
            report["whatsapp"]["skipped"] = "no_phone_on_inventory_managers"
        for to in phones:
            try:
                await _send_template(settings, template_message(to, check, last))
                report["whatsapp"]["sent"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad number must not stop the rest
                logger.warning("[floor-check] #%s: WhatsApp to %s failed: %s", check_id, to, exc)
                report["whatsapp"]["failed"].append({"phone": to, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - a notice must never undo a saved check
        logger.exception("[floor-check] #%s: notifying the inventory managers failed", check_id)
        report["error"] = str(exc)
    logger.info("[floor-check] #%s notice: %s", check_id, report)
    return report


# ── the tap ──────────────────────────────────────────────────────────────────

def parse_payload(payload: Optional[str]) -> Optional[tuple[str, int]]:
    """('approve' | 'reject', check_id) for one of our buttons, else None."""
    m = _PAYLOAD_RE.match((payload or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


async def _reply(wa: str, text: str) -> None:
    from app.modules.sample.services.whatsapp_service import _send_text  # lazy: avoid an import cycle
    try:
        await _send_text(wa, text)
    except Exception:  # noqa: BLE001 - a failed reply must not undo a recorded decision
        logger.exception("[floor-check-wa] reply to %s failed", wa)


async def handle_tap(conn, wa: str, payload: Optional[str]) -> Optional[dict[str, Any]]:
    """`wa` is the sender as handle_inbound formats it. None when the tap is not ours."""
    parsed = parse_payload(payload)
    if parsed is None:
        return None
    action, check_id = parsed
    result: dict[str, Any] = {"ok": False, "flow": "stock_take_floor_check",
                              "check_id": check_id, "action": action}
    try:
        from app.modules.auth.services import permission_service
        from app.modules.transfer.services.request_action import load_user  # lazy: avoid an import cycle

        # Who is asking comes first, so a number that is not an ERP user learns nothing.
        user = await load_user(conn, phone=wa)
        if user is None:
            await _reply(wa, NOT_REGISTERED)
            return {**result, "reason": "unknown_number"}
        if not await permission_service.check_permission(
                conn, user.role_ids, user.is_admin, "stock_take", None, None, "approve",
                entity=user.entity or None):
            await _reply(wa, "Your ERP account is not allowed to approve floor checks, so nothing was done.")
            return {**result, "reason": "not_permitted"}

        check = await fcs.get_check(conn, check_id)
        if check is None:
            await _reply(wa, f"Floor check #{check_id} was not found, so nothing was done.")
            return {**result, "reason": "not_found"}
        if not covers_place(user.allowed_warehouses, [], check["warehouse"], ""):
            await _reply(wa, f"You are not assigned to warehouse {check['warehouse']}, so nothing was done.")
            return {**result, "reason": "warehouse_not_allowed"}

        name = f"{check.get('floor_label') or check['floor_name']} ({check['warehouse']}), {_day(check['check_day'])}"
        try:
            async with conn.transaction():
                done = await fcs.decide(
                    conn, check_id=check_id, approve=(action == APPROVE),
                    reason=None if action == APPROVE else WA_REJECT_REASON,
                    actor=user.full_name or user.email or wa, user_id=user.user_id, via="whatsapp")
        except fcs.FloorCheckError as exc:
            await _reply(wa, f"Nothing was done for {name}: {exc.message}")
            return {**result, "reason": exc.code}
        if done["status"] == "approved":
            await _reply(wa, f"Approved: {name} - no stock movement, as reported by {done['checked_by']}.")
        else:
            await _reply(wa, f"Rejected: {name}. It shows as rejected on {SCREEN}, "
                             f"and the floor can check again and report it again today.")
        return {**result, "ok": True, "status": done["status"]}
    except Exception:  # noqa: BLE001 - own the tap: never fall through, never raise
        logger.exception("[floor-check-wa] check %s %s tap failed", check_id, action)
        await _reply(wa, f"Sorry, your answer could not be recorded, so nothing was done. "
                         f"Please decide in the ERP under {SCREEN}.")
        return {**result, "reason": "error"}
