"""Customer-Returns approval decision — idempotent status transition + audit.

Shared by the in-app decision endpoint and the emailed magic-link endpoint. Only
transitions from a non-terminal state (Pending / Submitted / On Hold); a terminal
state (Approved / Rejected) is reported as already-actioned, never re-applied.
Writes status + approved_by + approved_at (+ optional remark).
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.customer_returns.tables import cr_table_names

ACTION_TO_STATUS = {"approve": "Approved", "reject": "Rejected", "hold": "On Hold"}
_NON_TERMINAL = ["Pending", "Submitted", "On Hold"]


async def apply_cr_action(conn, company: str, cr_id: str, action: str, actor: str,
                          remark: str | None = None) -> dict:
    if action not in ACTION_TO_STATUS:
        raise HTTPException(400, detail="action must be approve, reject or hold")
    new_status = ACTION_TO_STATUS[action]
    header = cr_table_names(company)["header"]  # whitelisted (CFPL/CDPL) — safe to interpolate

    row = await conn.fetchrow(f"SELECT status FROM {header} WHERE rtv_id = $1", cr_id)
    if not row:
        raise HTTPException(404, detail="Customer return not found")
    if row["status"] not in _NON_TERMINAL:
        # Already decided (Approved/Rejected) — idempotent, don't re-apply.
        return {"rtv_id": cr_id, "status": row["status"], "already_actioned": True}

    updated = await conn.fetchrow(
        f"""
        UPDATE {header}
           SET status = $2, approved_by = $3, approved_at = NOW(),
               approval_remark = COALESCE($4, approval_remark), updated_at = NOW()
         WHERE rtv_id = $1 AND status = ANY($5::text[])
        RETURNING rtv_id, status
        """,
        cr_id, new_status, actor, remark, _NON_TERMINAL,
    )
    if not updated:
        # Lost a race between the SELECT and the guarded UPDATE.
        cur = await conn.fetchrow(f"SELECT status FROM {header} WHERE rtv_id = $1", cr_id)
        return {"rtv_id": cr_id, "status": (cur["status"] if cur else new_status), "already_actioned": True}
    return {"rtv_id": cr_id, "status": updated["status"], "already_actioned": False}
