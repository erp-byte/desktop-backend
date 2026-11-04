"""Deciding a transfer request from outside the ERP: a WhatsApp button
(request_wa_reply.py) or an emailed link (transfer/request_email_router.py).

Both make the decision through request_decision_service with the user who acted, so
every check the ERP makes applies unchanged:

  * the role permission the route's require_permission checks (transfer.requests.accept
    for the store, .approve for an inventory manager) - checked here with
    permission_service.check_permission, the call that dependency makes;
  * the store's warehouse lock on the supplying site, nobody deciding their own
    request, and the request still being at that step - checked by the decision
    service itself, with the request row locked.

A refusal, or a request that has moved on, changes nothing; the outcome says so in
plain words, with the state the request is in, for the WhatsApp reply or the page the
link opens. The user is loaded as validate_session loads a signed-in one (all their
roles, an admin flag on any of them, their warehouse lock); an account that could not
sign in cannot decide either.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.core.sites import canonical_site
from app.modules.auth.middleware import AuthUser, _authuser_from_session
from app.modules.auth.services import permission_service
from app.modules.auth.services.auth_service import _effective_roles
from app.modules.auth.services.phone import lookup_keys
from app.modules.transfer import permissions
from app.modules.transfer.services import query_service
from app.modules.transfer.services import request_decision_service as decisions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    stage: str              # who decides: 'store' | 'manager'
    needs: tuple[str, ...]  # the statuses the request may be at
    permission: str         # transfer.requests.<permission>, as the route checks it
    needs_reason: bool
    done: str               # what the request is once decided, in words


STEPS: dict[str, Step] = {
    "accept":       Step("store", ("Pending",), "accept", False, "accepted"),
    "store_hold":   Step("store", ("Pending",), "accept", True, "on hold"),
    "store_reject": Step("store", ("Pending",), "accept", True, "rejected"),
    "approve":      Step("manager", ("Accepted", "On Hold"), "approve", False, "approved"),
    "hold":         Step("manager", ("Accepted",), "approve", True, "on hold"),
    "reject":       Step("manager", ("Accepted", "On Hold"), "approve", True, "rejected"),
}

# A request's status as a sentence ending ("... transfer request REQ1 is <this>.").
_STATE = {
    "Pending": "still waiting for the store's answer",
    "Accepted": "already accepted by the store and waiting for an inventory manager",
    "On Hold": "already on hold by an inventory manager",
    "Approved": "already approved",
    "Rejected": "already rejected",
    "Transferred": "already dispatched",
    "Deleted": "deleted",
}
_NOT_PERMITTED = {
    "store": "Your ERP account is not allowed to answer transfer requests for the store",
    "manager": "Your ERP account is not allowed to approve, hold or reject transfer requests",
}


def state_words(status: Optional[str], store_decision: Optional[str] = None) -> str:
    status = status or "Pending"
    if status == "Pending" and store_decision == "Held":
        return "already on hold by the store"
    return _STATE.get(status, f"'{status}'")


def _outcome(ok: bool, kind: str, message: str, *, request_id: int, action: str,
             request_no: Optional[str] = None, status: Optional[str] = None) -> dict[str, Any]:
    return {"ok": ok, "kind": kind, "message": message, "request_id": request_id,
            "action": action, "request_no": request_no, "status": status}


def _no(request_no: Optional[str], request_id: int) -> str:
    return (request_no or "").strip() or f"#{request_id}"


# ── who ──────────────────────────────────────────────────────────────────────
_USER_SQL = """
    SELECT u.user_id, u.phone, u.full_name, u.email, u.entity, u.role_id,
           u.is_active, u.status, u.allowed_warehouses, u.allowed_floors, u.allowed_entities,
           r.role_name, r.is_admin
      FROM auth_user u
      LEFT JOIN auth_role r ON u.role_id = r.role_id
"""


async def load_user(conn, *, user_id: Optional[int] = None, phone: Optional[str] = None) -> Optional[AuthUser]:
    """The ERP user with this id, or with this phone number (any spelling of it,
    auth.services.phone.lookup_keys); None when there is none or the account could not
    sign in (inactive, or its status is not 'active' - validate_session's test)."""
    if user_id is not None:
        row = await conn.fetchrow(_USER_SQL + " WHERE u.user_id = $1", int(user_id))
    else:
        keys = lookup_keys(phone)
        if not keys:
            return None
        row = await conn.fetchrow(
            _USER_SQL + " WHERE u.phone = ANY($1::text[])"
                        " ORDER BY array_position($1::text[], u.phone), u.user_id LIMIT 1",
            keys)
    if not row or not row["is_active"] or (row["status"] and row["status"] != "active"):
        return None
    primary = ({"role_id": row["role_id"], "role_name": row["role_name"],
                "is_admin": row["is_admin"], "description": None}
               if row["role_id"] is not None else None)
    roles = await _effective_roles(conn, row["user_id"], primary)
    return _authuser_from_session({
        "user_id": row["user_id"], "phone": row["phone"], "full_name": row["full_name"],
        "email": row["email"], "entity": row["entity"], "role_id": row["role_id"],
        "role_name": row["role_name"], "role_ids": [r["role_id"] for r in roles],
        "is_admin": any(r["is_admin"] for r in roles) or bool(row["is_admin"]),
        "allowed_entities": list(row["allowed_entities"] or ([row["entity"]] if row["entity"] else [])),
        "allowed_warehouses": list(row["allowed_warehouses"] or []),
        "allowed_floors": list(row["allowed_floors"] or []),
    })


async def may_decide(conn, user: AuthUser, action: str) -> bool:
    """The route's role-permission check (require_permission("transfer", "requests",
    action=...)): the same check_permission call, with the user's own entity."""
    return await permission_service.check_permission(
        conn, user.role_ids, user.is_admin, "transfer", "requests", None,
        STEPS[action].permission, entity=user.entity or None)


# ── checks before asking for more ────────────────────────────────────────────
async def precheck(conn, user: AuthUser, request_id: int, action: str) -> tuple[Optional[dict], Optional[dict]]:
    """(refusal, request): whether this user could make this decision now, asked
    before a reason is asked for or a confirm page is shown, so nobody types a reason
    for a decision that will be refused. Advisory only: decide() makes the decision
    through the decision service, which checks everything again with the row locked.
    `refusal` is None when nothing stands in the way; `request` is query_service's
    get_request (None when the request does not exist)."""
    step = STEPS[action]
    if not await may_decide(conn, user, action):
        return _outcome(False, "not_permitted", f"{_NOT_PERMITTED[step.stage]}, so nothing was done.",
                        request_id=request_id, action=action), None
    try:
        req = await query_service.get_request(conn, request_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return _outcome(False, "not_found", f"Transfer request #{request_id} no longer exists, so nothing was done.",
                        request_id=request_id, action=action), None
    no = _no(req.get("request_no"), request_id)
    status = req.get("status") or "Pending"
    if status not in step.needs or (action == "store_hold" and req.get("store_decision") == "Held"):
        words = state_words(status, req.get("store_decision"))
        return _outcome(False, "moved_on", f"Nothing was done: transfer request {no} is {words}.",
                        request_id=request_id, action=action, request_no=no, status=status), req
    try:
        if step.stage == "store":
            permissions.assert_can_answer_request_from(user, req.get("from_warehouse"))
        else:
            permissions.assert_not_own_request(user, req.get("created_by"), req.get("store_decided_by"))
    except AuthError as exc:
        return _outcome(False, "forbidden", f"Nothing was done: {exc.message}",
                        request_id=request_id, action=action, request_no=no, status=req.get("status")), req
    return None, req


# ── the decision ─────────────────────────────────────────────────────────────
async def _call(conn, action: str, request_id: int, user: AuthUser, reason: Optional[str]) -> dict:
    if action == "accept":
        return await decisions.accept_request(conn, request_id, user)
    if action == "store_hold":
        return await decisions.store_hold_request(conn, request_id, user, reason or "")
    if action == "store_reject":
        return await decisions.store_reject_request(conn, request_id, user, reason or "")
    if action == "approve":
        return await decisions.approve_request(conn, request_id, user)
    if action == "hold":
        return await decisions.hold_request(conn, request_id, user, reason or "")
    if action == "reject":
        return await decisions.reject_request(conn, request_id, user, reason or "")
    raise ValueError(f"unknown transfer request action {action!r}")


def _done_message(action: str, out: dict, reason: Optional[str]) -> str:
    no = _no(out.get("request_no"), out.get("id"))
    if action == "accept":
        return (f"Done. Transfer request {no} is accepted. "
                "It now goes to the inventory managers for approval.")
    if action == "approve":
        return (f"Done. Transfer request {no} is approved. The requester will be told, and the store at "
                f"{canonical_site(out.get('from_warehouse')) or 'the supplying site'} can now send it.")
    if action == "store_hold":
        return (f"Done. Transfer request {no} is on hold. Reason: {reason}. "
                "The requester and the inventory managers will be told.")
    if action == "hold":
        return f"Done. Transfer request {no} is on hold. Reason: {reason}. The requester will be told."
    who = "the store" if action == "store_reject" else "the inventory manager"
    return f"Done. Transfer request {no} is rejected by {who}. Reason: {reason}. The requester will be told."


async def decide(conn, user: AuthUser, request_id: int, action: str,
                 reason: Optional[str] = None) -> dict[str, Any]:
    """Make the decision as `user`, through the decision service. Returns the outcome
    ({ok, kind, message, request_id, action, request_no, status}); never raises for a
    refusal. kind is 'done', or why nothing was done: 'not_permitted', 'forbidden'
    (warehouse lock / own request), 'moved_on', 'not_found' or 'reason_required'."""
    step = STEPS[action]
    if not await may_decide(conn, user, action):
        return _outcome(False, "not_permitted", f"{_NOT_PERMITTED[step.stage]}, so nothing was done.",
                        request_id=request_id, action=action)
    reason = (reason or "").strip() or None
    try:
        out = await _call(conn, action, request_id, user, reason)
    except AuthError as exc:
        return _outcome(False, "forbidden", f"Nothing was done: {exc.message}",
                        request_id=request_id, action=action)
    except HTTPException as exc:
        if exc.status_code == 404:
            return _outcome(False, "not_found",
                            f"Transfer request #{request_id} no longer exists, so nothing was done.",
                            request_id=request_id, action=action)
        if exc.status_code == 409:
            # The request has moved on (decided in the ERP, or by someone else): say
            # what it is now, read after the refusal.
            row = await conn.fetchrow(
                "SELECT request_no, status, store_decision FROM interunit_transfer_requests WHERE id = $1",
                request_id)
            no = _no(row["request_no"] if row else None, request_id)
            status = (row["status"] if row else None) or "Pending"
            words = state_words(status, row["store_decision"] if row else None)
            return _outcome(False, "moved_on", f"Nothing was done: transfer request {no} is {words}.",
                            request_id=request_id, action=action, request_no=no, status=status)
        if exc.status_code == 400:
            return _outcome(False, "reason_required", "A reason is needed, so nothing was done.",
                            request_id=request_id, action=action)
        raise
    logger.info("[trq-action] request %s %s by %s", request_id, step.done, user.email or user.user_id)
    return _outcome(True, "done", _done_message(action, out, reason), request_id=request_id, action=action,
                    request_no=_no(out.get("request_no"), request_id), status=out.get("status"))
