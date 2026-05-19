"""DELETE /api/v1/po/{transaction_no}?reason=... implementation.

Soft-delete (sets deleted_at, deleted_by, delete_reason; mirrors is_deleted).
Spec gates:
    • 404 if not found.
    • 409 if already deleted.
    • 403 StoreHeadApprovalRequired if po_box count > 0 AND actor is not
      STORE_HEAD or ADMIN.
    • Records the action in po_event_log.
    • Returns dependent_records counts (dock_arrivals, grns, po_boxes).

`dock_arrivals` table doesn't exist in the current schema — `_safe_count`
returns 0 if the table is missing. `grn` and `po_box` exist.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

from core.middleware.request_context import AuthError
from modules.purchase.services import po_event_log

logger = logging.getLogger(__name__)


_STORE_HEAD_ROLES = frozenset({"store_head", "admin"})


async def _required_count(conn, sql: str, *params) -> int:
    """COUNT(*) on a table that MUST exist. Lets DB errors propagate.

    HI-04: a swallowed DB error on po_box would silently skip the
    STORE_HEAD gate and return wrong dependent_records — caller would
    happily soft-delete a PO with weighed boxes.
    """
    n = await conn.fetchval(sql, *params)
    return int(n or 0)


async def _optional_count(conn, sql: str, *params) -> int:
    """COUNT(*) on a table that may not exist yet (e.g. dock_arrival).
    Returns 0 ONLY if the table is missing (UndefinedTableError); any
    other failure propagates."""
    try:
        n = await conn.fetchval(sql, *params)
        return int(n or 0)
    except asyncpg.UndefinedTableError as e:
        logger.info("po_delete._optional_count: missing table — %s", e)
        return 0


async def soft_delete(
    pool,
    *,
    transaction_no: str,
    reason: str,
    actor_user_id: str,
    actor_role: str,
    actor_is_admin: bool,
) -> dict:
    if not reason or not reason.strip():
        raise AuthError("validation_error", "reason is required", 400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT transaction_no, entity, po_number, deleted_at
                  FROM po_header
                 WHERE transaction_no = $1
                FOR UPDATE
                """,
                transaction_no,
            )
            if row is None:
                raise AuthError("not_found", "Purchase order not found", 404)
            if row["deleted_at"] is not None:
                raise AuthError(
                    "already_deleted",
                    "Purchase order is already deleted",
                    409,
                )

            entity = row["entity"]

            # Dependent counts. po_box and grn are required tables (real
            # errors propagate); dock_arrival isn't created yet so a missing
            # table is silently treated as 0.
            box_count = await _required_count(
                conn,
                "SELECT COUNT(*) FROM po_box WHERE transaction_no = $1",
                transaction_no,
            )
            grn_count = await _required_count(
                conn,
                "SELECT COUNT(*) FROM grn WHERE transaction_no = $1",
                transaction_no,
            )
            dock_count = await _optional_count(
                conn,
                "SELECT COUNT(*) FROM dock_arrival WHERE transaction_no = $1",
                transaction_no,
            )

            if box_count > 0:
                role_lower = (actor_role or "").lower()
                if not actor_is_admin and role_lower not in _STORE_HEAD_ROLES:
                    raise AuthError(
                        "store_head_approval_required",
                        "Stores has weighed goods against this PO. STORE_HEAD or ADMIN approval required.",
                        403,
                        details={
                            "po_box_count": box_count,
                            "required_roles": ["STORE_HEAD", "ADMIN"],
                        },
                    )

            # HI-03: po_header's row lock does NOT prevent INSERTs into
            # po_box (different table). Re-COUNT immediately before the
            # UPDATE; if a stores-team box landed since the gate check,
            # demand approval. Same query, last-known-good check.
            box_count_recheck = await _required_count(
                conn,
                "SELECT COUNT(*) FROM po_box WHERE transaction_no = $1",
                transaction_no,
            )
            if box_count_recheck > box_count:
                role_lower = (actor_role or "").lower()
                if not actor_is_admin and role_lower not in _STORE_HEAD_ROLES:
                    raise AuthError(
                        "store_head_approval_required",
                        "Stores weighed boxes against this PO during the delete request. "
                        "STORE_HEAD or ADMIN approval required.",
                        403,
                        details={
                            "po_box_count": box_count_recheck,
                            "po_box_count_at_gate": box_count,
                            "required_roles": ["STORE_HEAD", "ADMIN"],
                            "race_detected": True,
                        },
                    )
                box_count = box_count_recheck

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE po_header
                   SET deleted_at    = $2,
                       deleted_by    = $3,
                       delete_reason = $4,
                       is_deleted    = TRUE
                 WHERE transaction_no = $1
                """,
                transaction_no, now, actor_user_id, reason.strip(),
            )

            await po_event_log.write(
                conn,
                transaction_no=transaction_no,
                entity=entity,
                event_type=po_event_log.EVENT_DELETED,
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                reason=reason.strip(),
                payload={
                    "dependent_records": {
                        "po_boxes": box_count,
                        "grns": grn_count,
                        "dock_arrivals": dock_count,
                    },
                },
            )

    return {
        "transaction_no": transaction_no,
        "entity": entity,
        "po_number": row["po_number"],
        "deleted_at": now.isoformat().replace("+00:00", "Z"),
        "deleted_by": actor_user_id,
        "delete_reason": reason.strip(),
        "dependent_records": {
            "dock_arrivals": dock_count,
            "grns": grn_count,
            "po_boxes": box_count,
        },
    }
