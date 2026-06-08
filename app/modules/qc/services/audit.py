"""QC Inward Inspection — audit trail helpers.

Thin wrappers around qc_inward_inspection_audit:
  write_audit  — INSERT one event (called within an open transaction)
  list_audit   — SELECT ordered history for one inspection
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.core.helpers import insert_with_pk_retry, new_short_time_id


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        try:
            s = v.isoformat()
            return s.replace("+00:00", "Z") if "T" in s else s
        except (TypeError, ValueError):
            return str(v)
    return str(v)


async def write_audit(
    conn,
    *,
    inspection_id: int,
    event_type: str,
    from_state: str | None,
    to_state: str | None,
    actor_user_id: int | None,
    payload_diff: dict | None = None,
) -> None:
    """INSERT one audit event.  Must be called inside an open transaction."""
    diff_json: str | None = json.dumps(payload_diff) if payload_diff is not None else None

    # audit_id is an app-supplied 8-digit time-based BIGINT; PK retry handles
    # the rare same-millisecond collision.
    async def _insert_audit():
        return await conn.execute(
            """
            INSERT INTO qc_inward_inspection_audit
                (audit_id, inspection_id, event_type, from_state, to_state,
                 actor_user_id, payload_diff, occurred_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW())
            """,
            new_short_time_id(),
            inspection_id,
            event_type,
            from_state,
            to_state,
            actor_user_id,
            diff_json,
        )

    await insert_with_pk_retry(conn, _insert_audit)


async def list_audit(pool, inspection_id: int) -> list[dict]:
    """Return all audit events for *inspection_id* ordered by occurred_at."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT event_type, from_state, to_state, occurred_at,
                   actor_user_id, payload_diff
              FROM qc_inward_inspection_audit
             WHERE inspection_id = $1
             ORDER BY occurred_at, audit_id
            """,
            inspection_id,
        )
    return [
        {
            "event_type": r["event_type"],
            "from_state": r["from_state"],
            "to_state": r["to_state"],
            "occurred_at": _iso(r["occurred_at"]),
            "actor_user_id": r["actor_user_id"],
            "payload_diff": (
                r["payload_diff"]
                if isinstance(r["payload_diff"], dict)
                else (
                    json.loads(r["payload_diff"])
                    if isinstance(r["payload_diff"], str)
                    else None
                )
            ),
        }
        for r in rows
    ]
