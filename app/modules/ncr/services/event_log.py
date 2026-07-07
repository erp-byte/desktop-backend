"""Append-only event log for NCR. Best-effort — never raises."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Spec event types — keep in sync with the audit dropdown values.
EV_RAISED = "ncr_raised"
EV_RAISED_VIA_OVERRIDE = "ncr_raised_via_override"
EV_DISPOSITIONED = "ncr_dispositioned"
EV_DISPOSITION_EDITED = "ncr_disposition_edited"
EV_HEADER_UPDATED = "ncr_header_updated"
EV_CAPA_SUBMITTED = "ncr_capa_submitted"
EV_CAPA_UPDATED = "ncr_capa_updated"
EV_CAPA_DELETED = "ncr_capa_deleted"
EV_VERIFIED_EFFECTIVE = "ncr_verified_effective"
EV_VERIFIED_INEFFECTIVE = "ncr_verified_ineffective"
EV_CANCELLED = "ncr_cancelled"
EV_CANCELLED_FOR_OVERRIDE = "ncr_cancelled_for_override"
EV_CANCELLED_FOR_INSPECTION_REOPEN = "ncr_cancelled_for_inspection_reopen"
EV_REOPENED = "ncr_reopened"
EV_CLOSED = "ncr_closed"
EV_DUAL_APPROVED = "ncr_dual_approved"


async def write(
    conn,
    *,
    ncr_no: str,
    event_type: str,
    occurred_by: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    try:
        await conn.execute(
            """
            INSERT INTO ncr_event_log
                (event_type, entity_type, entity_id, occurred_by, payload)
            VALUES ($1, 'ncr', $2, $3, $4::jsonb)
            """,
            event_type, ncr_no, occurred_by,
            json.dumps(payload, default=str) if payload else None,
        )
    except Exception as e:
        logger.warning(
            "ncr_event_log.write_failed ncr_no=%s event=%s err=%r",
            ncr_no, event_type, e,
        )
