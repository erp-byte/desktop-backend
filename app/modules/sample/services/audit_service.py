"""Sample audit log — one row per mutation (checklist A11 / spec §9.8).

Every mutating sample service calls write_audit() so a requisition's full
history (status changes, approvals, article edits, conversions, prints, voids)
is reconstructable from sample_audit_log. Keeping this in one helper means no
service hand-writes audit INSERTs.
"""
from __future__ import annotations

import json
from typing import Any

# event_type vocabulary (open, but keep these canonical)
EV_STATUS_CHANGE   = "STATUS_CHANGE"
EV_APPROVAL        = "APPROVAL"
EV_ARTICLE_EDIT    = "ARTICLE_EDIT"
EV_OUTWARD         = "OUTWARD"
EV_JOBCARD         = "JOBCARD"
EV_CONVERSION      = "CONVERSION"
EV_GATE_PASS       = "GATE_PASS"
EV_GATE_PASS_PRINT = "GATE_PASS_PRINT"
EV_VOID            = "VOID"
EV_CANCEL          = "CANCEL"
EV_NPD             = "NPD"


async def write_audit(
    conn,
    requisition_id: int,
    event_type: str,
    *,
    old_value: Any | None = None,
    new_value: Any | None = None,
    actor_user_id: int | None = None,
    actor_role: str | None = None,
    remarks: str | None = None,
) -> None:
    """Insert one sample_audit_log row.

    old_value / new_value are serialised to JSONB. Cast to ::jsonb so asyncpg
    sends them as text and Postgres parses — no codec registration required.
    """
    await conn.execute(
        """
        INSERT INTO sample_audit_log
            (requisition_id, event_type, old_value, new_value,
             actor_user_id, actor_role, remarks)
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7)
        """,
        requisition_id,
        event_type,
        json.dumps(old_value) if old_value is not None else None,
        json.dumps(new_value) if new_value is not None else None,
        actor_user_id,
        actor_role,
        remarks,
    )
