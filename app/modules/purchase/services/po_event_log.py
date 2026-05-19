"""Append-only event log writer for the PO endpoints.

Writes to `po_event_log` (created by 005_po_rebuild.sql). Never raises on
failure — auditing is best-effort, never the reason a request 500s. If the
log write fails (table missing, DB blip), we log a warning and continue.

Caller should pass a connection that's already inside whatever transaction
makes sense (e.g. inside the soft-delete txn so the event row commits with
the delete; or autocommit for create/update).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

EVENT_CREATED = "created"
EVENT_UPDATED = "updated"
EVENT_DELETED = "soft_deleted"
EVENT_RESTORED = "restored"


async def write(
    conn,
    *,
    transaction_no: str,
    entity: str,
    event_type: str,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        await conn.execute(
            """
            INSERT INTO po_event_log
                (transaction_no, entity, event_type, actor_user_id, actor_role, reason, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            transaction_no,
            entity,
            event_type,
            actor_user_id,
            actor_role,
            reason,
            json.dumps(payload, default=str) if payload else None,
        )
    except Exception as e:
        # Audit must never break the request. If the table is missing or the
        # write fails for any reason, log it for ops and move on.
        logger.warning(
            "po_event_log.write_failed transaction_no=%s event=%s err=%r",
            transaction_no, event_type, e,
        )
