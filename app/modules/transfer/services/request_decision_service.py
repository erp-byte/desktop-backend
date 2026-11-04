"""Store and inventory-manager decisions on a transfer request.

    Pending → Accepted → Approved → Transferred

The supplying store answers a Pending request (accept / reject); an inventory
manager then decides an Accepted one (approve / reject). Rejected is terminal.
'Transferred' is written by create_service when the dispatch is created, which
is also where "only an Approved request can be dispatched" is enforced.

Either side may also hold, with a reason (migration 127):
  store hold    a Pending request stays 'Pending' with store_decision 'Held' and the
                reason in store_hold_reason; the store can still accept or reject it,
                but not hold it again (409).
  manager hold  an Accepted request becomes 'On Hold' with manager_decision 'Held' and
                the reason in manager_hold_reason; an inventory manager can still
                approve or reject it, but not hold it again (409).
A later accept / approve / reject overwrites the decision, its by and its at; the
*_hold_reason columns stay as history.

Each decision runs in one transaction with the request row locked: status check,
the row-dependent permission rules (permissions.py), then the write. The role
permission itself (transfer.requests.accept / .approve) is the route's
`require_permission` dependency. The response is read back through
query_service.get_request, so it is the same envelope as GET /requests/{id}.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.modules.auth.middleware import AuthUser
from app.modules.transfer import permissions
from app.modules.transfer.services import query_service, request_notify


HELD = "Held"            # store_decision / manager_decision of a hold
ON_HOLD = "On Hold"      # status of a request an inventory manager has put on hold


def _reason(reason: str | None, what: str = "reject a request") -> str:
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(400, f"A reason is required to {what}")
    return reason


async def _lock_request(conn, request_id: int, *required_status: str) -> dict:
    """Lock the request row until the caller's transaction ends and check its status
    is one of `required_status`."""
    row = await conn.fetchrow(
        """
        SELECT from_site, status, created_by, store_decided_by, store_decision
        FROM interunit_transfer_requests
        WHERE id = $1
        FOR UPDATE
        """,
        request_id,
    )
    if not row:
        raise HTTPException(404, "Request not found")
    status = row["status"] or "Pending"
    if status not in required_status:
        allowed = " or ".join(f"'{s}'" for s in required_status)
        raise HTTPException(
            409, f"Request is '{status}'; this can only be done while it is {allowed}")
    return dict(row)


async def _store_decision(conn, request_id: int, user: AuthUser, decision: str,
                          reason: str | None = None) -> dict:
    # The decision ('Accepted' / 'Rejected') is also the request's new status. A
    # store-held request is still Pending, so it is answered the same way.
    async with conn.transaction():
        req = await _lock_request(conn, request_id, "Pending")
        permissions.assert_can_answer_request_from(user, req["from_site"])
        await conn.execute(
            """
            UPDATE interunit_transfer_requests
            SET store_decision = $1, store_decided_by = $2, store_decided_at = $3,
                store_reject_reason = $4, status = $1, updated_at = $3
            WHERE id = $5
            """,
            decision, user.email or "unknown", datetime.now(), reason, request_id,
        )
        return await query_service.get_request(conn, request_id)


async def _manager_decision(conn, request_id: int, user: AuthUser, decision: str,
                            reason: str | None = None) -> dict:
    # The decision ('Approved' / 'Rejected') is also the request's new status. An
    # 'On Hold' request is decided the same way as an Accepted one.
    async with conn.transaction():
        req = await _lock_request(conn, request_id, "Accepted", ON_HOLD)
        permissions.assert_not_own_request(user, req["created_by"], req["store_decided_by"])
        await conn.execute(
            """
            UPDATE interunit_transfer_requests
            SET manager_decision = $1, manager_decided_by = $2, manager_decided_at = $3,
                manager_reject_reason = $4, status = $1, updated_at = $3
            WHERE id = $5
            """,
            decision, user.email or "unknown", datetime.now(), reason, request_id,
        )
        return await query_service.get_request(conn, request_id)


async def _store_hold(conn, request_id: int, user: AuthUser, reason: str) -> dict:
    # The request stays 'Pending': only the store's answer says it is held.
    async with conn.transaction():
        req = await _lock_request(conn, request_id, "Pending")
        if req["store_decision"] == HELD:
            raise HTTPException(
                409, "Request is already on hold by the store; it can now only be accepted or rejected")
        permissions.assert_can_answer_request_from(user, req["from_site"])
        await conn.execute(
            """
            UPDATE interunit_transfer_requests
            SET store_decision = $1, store_decided_by = $2, store_decided_at = $3,
                store_hold_reason = $4, updated_at = $3
            WHERE id = $5
            """,
            HELD, user.email or "unknown", datetime.now(), reason, request_id,
        )
        return await query_service.get_request(conn, request_id)


async def _manager_hold(conn, request_id: int, user: AuthUser, reason: str) -> dict:
    # Accepted -> 'On Hold' (holding an On Hold request again is the 409 of the gate).
    async with conn.transaction():
        req = await _lock_request(conn, request_id, "Accepted")
        permissions.assert_not_own_request(user, req["created_by"], req["store_decided_by"])
        await conn.execute(
            """
            UPDATE interunit_transfer_requests
            SET manager_decision = $1, manager_decided_by = $2, manager_decided_at = $3,
                manager_hold_reason = $4, status = $5, updated_at = $3
            WHERE id = $6
            """,
            HELD, user.email or "unknown", datetime.now(), reason, ON_HOLD, request_id,
        )
        return await query_service.get_request(conn, request_id)


# Each decision, once committed, tells the next person (request_notify: in the
# background, best-effort - it never fails or undoes the decision).
async def accept_request(conn, request_id: int, user: AuthUser) -> dict:
    out = await _store_decision(conn, request_id, user, "Accepted")
    request_notify.notify("accepted", request_id)
    return out


async def store_reject_request(conn, request_id: int, user: AuthUser, reason: str) -> dict:
    out = await _store_decision(conn, request_id, user, "Rejected", _reason(reason))
    request_notify.notify("rejected", request_id)
    return out


async def store_hold_request(conn, request_id: int, user: AuthUser, reason: str) -> dict:
    out = await _store_hold(conn, request_id, user, _reason(reason, "put a request on hold"))
    request_notify.notify("store_held", request_id)
    return out


async def approve_request(conn, request_id: int, user: AuthUser) -> dict:
    out = await _manager_decision(conn, request_id, user, "Approved")
    request_notify.notify("approved", request_id)
    return out


async def reject_request(conn, request_id: int, user: AuthUser, reason: str) -> dict:
    out = await _manager_decision(conn, request_id, user, "Rejected", _reason(reason))
    request_notify.notify("rejected", request_id)
    return out


async def hold_request(conn, request_id: int, user: AuthUser, reason: str) -> dict:
    out = await _manager_hold(conn, request_id, user, _reason(reason, "put a request on hold"))
    request_notify.notify("manager_held", request_id)
    return out
