"""Partial-update + soft-delete logic for job cards and their annexures.

Owns 'fix-up' semantics — distinct from job_card_engine.py which owns
lifecycle transitions (start, complete, sign-off, etc.).
"""

from typing import Any, Mapping
from fastapi import HTTPException
import asyncpg


# ─── Allow-lists ────────────────────────────────────────────────────────────
# Per-table whitelist of editable columns. Keys not in this set are silently
# dropped before SQL is built — defends against client-supplied junk fields
# AND prevents updates to identity / system-managed columns.

JOB_CARD_EDITABLE_COLS = frozenset({
    "machine_id", "assigned_to_team_leader", "team_members",
    "factory", "floor",
    "customer_name", "batch_number", "batch_size_kg",
    "bom_id", "process_name", "stage",
})

ENVIRONMENT_EDITABLE_COLS         = frozenset({"parameter_name", "value"})
METAL_DETECTION_EDITABLE_COLS     = frozenset({"check_type", "fe_pass", "nfe_pass", "ss_pass", "failed_units", "remarks"})
WEIGHT_CHECK_EDITABLE_COLS        = frozenset({"sample_number", "net_weight", "gross_weight", "leak_test_pass"})
LOSS_RECONCILIATION_EDITABLE_COLS = frozenset({"loss_category", "budgeted_loss_pct", "budgeted_loss_kg", "actual_loss_kg", "variance_kg", "remarks"})
REMARKS_EDITABLE_COLS             = frozenset({"remark_type", "content"})

# R1: Header-edit (machine, team, batch_number, factory, floor, BOM swap)
# is allowed through `material_received` but BLOCKED once a JC is running.
# Operational data entry (output, consumption, QC) is the only way to change
# state from `in_progress` onward. CANCELLABLE_STATUSES already matches the
# spec - keeping the existing set.
EDITABLE_STATUSES    = frozenset({"locked", "unlocked", "assigned", "material_received"})
CANCELLABLE_STATUSES = frozenset({"locked", "unlocked", "assigned"})


# ─── Generic helpers ────────────────────────────────────────────────────────

async def _apply_partial_update(
    conn: asyncpg.Connection, *,
    table: str, pk_col: str, pk_val: int,
    payload: Mapping[str, Any],
    allowed_cols: frozenset, updated_by: str,
    parent_jc_id: int | None = None,
) -> tuple[dict, list[str]]:
    """Build & execute UPDATE for only the supplied + allowed columns.

    If parent_jc_id is given, the WHERE clause also enforces job_card_id match
    (used for annexure rows so a guessed env_id can't bypass URL ownership).

    Returns (updated_row_dict, list_of_changed_column_names).
    Raises 404 if row missing/deleted, 422 if no valid columns supplied.
    """
    fields = {k: v for k, v in payload.items() if k in allowed_cols}
    if not fields:
        raise HTTPException(status_code=422, detail="No editable fields supplied")

    set_parts: list[str] = []
    params: list = []
    for col, val in fields.items():
        set_parts.append(f"{col} = ${len(params) + 1}")
        params.append(val)

    set_parts.append("updated_at = NOW()")
    set_parts.append(f"updated_by = ${len(params) + 1}")
    params.append(updated_by)

    where_parts = [f"{pk_col} = ${len(params) + 1}", "deleted_at IS NULL"]
    params.append(pk_val)
    if parent_jc_id is not None:
        where_parts.append(f"job_card_id = ${len(params) + 1}")
        params.append(parent_jc_id)

    sql = (
        f"UPDATE {table} SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)} "
        f"RETURNING *"
    )
    row = await conn.fetchrow(sql, *params)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{table} row not found or already deleted")
    return dict(row), list(fields.keys())


async def _apply_soft_delete(
    conn: asyncpg.Connection, *,
    table: str, pk_col: str, pk_val: int,
    deleted_by: str, reason: str | None = None,
    parent_jc_id: int | None = None,
) -> dict:
    set_parts = ["deleted_at = NOW()", "deleted_by = $1"]
    params: list = [deleted_by]
    if reason is not None:
        set_parts.append(f"cancellation_reason = ${len(params) + 1}")
        params.append(reason)
        set_parts.append("status = 'cancelled'")

    where_parts = [f"{pk_col} = ${len(params) + 1}", "deleted_at IS NULL"]
    params.append(pk_val)
    if parent_jc_id is not None:
        where_parts.append(f"job_card_id = ${len(params) + 1}")
        params.append(parent_jc_id)

    sql = (
        f"UPDATE {table} SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)} "
        f"RETURNING *"
    )
    row = await conn.fetchrow(sql, *params)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{table} row not found or already deleted")
    return dict(row)


async def _verify_parent_jc_editable(conn, job_card_id: int) -> dict:
    jc = await conn.fetchrow(
        "SELECT status, entity, job_card_number FROM job_card "
        "WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if jc is None:
        raise HTTPException(404, "Job card not found")
    if jc["status"] not in EDITABLE_STATUSES:
        raise HTTPException(409, f"Job card status '{jc['status']}' is not editable")
    return dict(jc)


# ─── Main job card ──────────────────────────────────────────────────────────

async def patch_job_card(conn, job_card_id: int, payload: dict) -> tuple[dict, dict, list[str]]:
    """Returns (jc_meta_dict, updated_row_dict, changed_fields).

    jc_meta_dict has keys: status, entity, job_card_number — for the router's
    webhook emission step.
    """
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card", pk_col="job_card_id", pk_val=job_card_id,
        payload=payload, allowed_cols=JOB_CARD_EDITABLE_COLS, updated_by=updated_by,
    )
    return jc, row, changed


async def cancel_job_card(conn, job_card_id: int, *, reason: str, deleted_by: str) -> tuple[dict, dict]:
    """Returns (jc_meta_dict, soft_deleted_row_dict)."""
    jc = await conn.fetchrow(
        "SELECT status, entity, job_card_number FROM job_card "
        "WHERE job_card_id = $1 AND deleted_at IS NULL",
        job_card_id,
    )
    if jc is None:
        raise HTTPException(404, "Job card not found")
    if jc["status"] not in CANCELLABLE_STATUSES:
        raise HTTPException(409, f"Cannot cancel — status '{jc['status']}'. Use force-unlock + close instead.")
    row = await _apply_soft_delete(
        conn, table="job_card", pk_col="job_card_id", pk_val=job_card_id,
        deleted_by=deleted_by, reason=reason,
    )
    return dict(jc), row


# ─── Environment ────────────────────────────────────────────────────────────

async def patch_environment(conn, job_card_id: int, env_id: int, payload: dict
                            ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_environment", pk_col="env_id", pk_val=env_id,
        payload=payload, allowed_cols=ENVIRONMENT_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_environment(conn, job_card_id: int, env_id: int, deleted_by: str
                             ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_environment", pk_col="env_id", pk_val=env_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row


# ─── Metal Detection ────────────────────────────────────────────────────────

async def patch_metal_detection(conn, job_card_id: int, detection_id: int, payload: dict
                                ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_metal_detection", pk_col="detection_id", pk_val=detection_id,
        payload=payload, allowed_cols=METAL_DETECTION_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_metal_detection(conn, job_card_id: int, detection_id: int, deleted_by: str
                                 ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_metal_detection", pk_col="detection_id", pk_val=detection_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row


# ─── Weight Check ───────────────────────────────────────────────────────────

async def patch_weight_check(conn, job_card_id: int, check_id: int, payload: dict
                             ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_weight_check", pk_col="check_id", pk_val=check_id,
        payload=payload, allowed_cols=WEIGHT_CHECK_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_weight_check(conn, job_card_id: int, check_id: int, deleted_by: str
                              ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_weight_check", pk_col="check_id", pk_val=check_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row


# ─── Loss Reconciliation ────────────────────────────────────────────────────

async def patch_loss_reconciliation(conn, job_card_id: int, recon_id: int, payload: dict
                                    ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_loss_reconciliation", pk_col="recon_id", pk_val=recon_id,
        payload=payload, allowed_cols=LOSS_RECONCILIATION_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_loss_reconciliation(conn, job_card_id: int, recon_id: int, deleted_by: str
                                     ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_loss_reconciliation", pk_col="recon_id", pk_val=recon_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row


# ─── Remarks ────────────────────────────────────────────────────────────────

async def patch_remark(conn, job_card_id: int, remark_id: int, payload: dict
                       ) -> tuple[dict, dict, list[str]]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    updated_by = payload.pop("updated_by")
    row, changed = await _apply_partial_update(
        conn, table="job_card_remarks", pk_col="remark_id", pk_val=remark_id,
        payload=payload, allowed_cols=REMARKS_EDITABLE_COLS,
        updated_by=updated_by, parent_jc_id=job_card_id,
    )
    return jc, row, changed


async def delete_remark(conn, job_card_id: int, remark_id: int, deleted_by: str
                        ) -> tuple[dict, dict]:
    jc = await _verify_parent_jc_editable(conn, job_card_id)
    row = await _apply_soft_delete(
        conn, table="job_card_remarks", pk_col="remark_id", pk_val=remark_id,
        deleted_by=deleted_by, parent_jc_id=job_card_id,
    )
    return jc, row
