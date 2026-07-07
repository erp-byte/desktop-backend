"""QC Inward Inspection — core inspection lifecycle service.

Covers:
  list_inspections        — paginated list with rich filter support
  get_detail              — full inspection + nested readings
  list_pending_intimations — picker for start-inspection form
  start                   — create inspection from a pending intimation
  cancel                  — cancel an in-progress inspection
  reopen                  — reopen a cancelled inspection
  update_header           — partial update of header fields
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.core.middleware.request_context import AuthError
from app.modules.qc.services.audit import write_audit


# ── ISO helpers ──────────────────────────────────────────────────────────


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


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── decision helper ──────────────────────────────────────────────────────


def _compute_decision(verdict: str | None, status: str | None) -> str:
    """Map verdict + status to a human-facing decision label."""
    if verdict == "passed" or status == "verdict_passed":
        return "approved"
    if verdict == "failed" or status == "verdict_failed":
        return "rejected"
    if status == "cancelled":
        return "cancelled"
    return "hold"


# ── row → dict helpers ───────────────────────────────────────────────────


def _row_to_list_item(row) -> dict:
    verdict = row["verdict"] if "verdict" in row.keys() else None
    status = row["status"] if "status" in row.keys() else None
    return {
        "inspection_id": row["inspection_id"],
        "po_number": row["po_number"],
        "transaction_no": row["transaction_no"],
        "sku_name": row["sku_name"],
        "sku_name_raw": row["sku_name_raw"] if "sku_name_raw" in row.keys() else None,
        "sku_id": row["sku_id"],
        "vehicle_no": row["vehicle_no"] if "vehicle_no" in row.keys() else None,
        "warehouse": row["warehouse"] if "warehouse" in row.keys() else None,
        "verdict": verdict,
        "status": status,
        "decision": _compute_decision(verdict, status),
        "started_at": _iso(row["started_at"]) if "started_at" in row.keys() else None,
        "supplier_name": row["supplier_name"] if "supplier_name" in row.keys() else None,
        "lot_number": row["lot_number"] if "lot_number" in row.keys() else None,
        "inspection_ref": row["inspection_ref"] if "inspection_ref" in row.keys() else None,
    }


def _row_to_detail(row) -> dict:
    verdict = row["verdict"] if "verdict" in row.keys() else None
    status = row["status"] if "status" in row.keys() else None
    return {
        "inspection_id": row["inspection_id"],
        "inspection_ref": row["inspection_ref"] if "inspection_ref" in row.keys() else None,
        "qc_intimation_id": row["qc_intimation_id"] if "qc_intimation_id" in row.keys() else None,
        "status": status,
        "verdict": verdict,
        "decision": _compute_decision(verdict, status),
        "sample_size": row["sample_size"] if "sample_size" in row.keys() else None,
        "inspection_method": row["inspection_method"] if "inspection_method" in row.keys() else None,
        "inspector_user_id": row["inspector_user_id"] if "inspector_user_id" in row.keys() else None,
        "started_at": _iso(row["started_at"]) if "started_at" in row.keys() else None,
        "started_by": row["started_by"] if "started_by" in row.keys() else None,
        "started_by_name": row["started_by_name"] if "started_by_name" in row.keys() else None,
        "verdict_at": _iso(row["verdict_at"]) if "verdict_at" in row.keys() else None,
        "accepted_qty": _f(row["accepted_qty"]) if "accepted_qty" in row.keys() else None,
        "rejected_qty": _f(row["rejected_qty"]) if "rejected_qty" in row.keys() else None,
        "ncr_no": row["ncr_no"] if "ncr_no" in row.keys() else None,
        "cancelled_at": _iso(row["cancelled_at"]) if "cancelled_at" in row.keys() else None,
        "cancelled_by": row["cancelled_by"] if "cancelled_by" in row.keys() else None,
        "cancelled_by_name": row["cancelled_by_name"] if "cancelled_by_name" in row.keys() else None,
        "cancel_reason": row["cancel_reason"] if "cancel_reason" in row.keys() else None,
        "reopened_at": _iso(row["reopened_at"]) if "reopened_at" in row.keys() else None,
        "reopen_reason": row["reopen_reason"] if "reopen_reason" in row.keys() else None,
        "verdict_overridden_by": row["verdict_overridden_by"] if "verdict_overridden_by" in row.keys() else None,
        "verdict_overridden_by_name": row["verdict_overridden_by_name"] if "verdict_overridden_by_name" in row.keys() else None,
        "override_reason": row["override_reason"] if "override_reason" in row.keys() else None,
        # Approval (manager's verdict sign-off) — migration 038
        "approved_by": row["approved_by"] if "approved_by" in row.keys() else None,
        "approved_by_name": row["approved_by_name"] if "approved_by_name" in row.keys() else None,
        "approved_at": _iso(row["approved_at"]) if "approved_at" in row.keys() else None,
        "remarks": row["remarks"] if "remarks" in row.keys() else None,
        # Denormalised
        "po_number": row["po_number"] if "po_number" in row.keys() else None,
        "transaction_no": row["transaction_no"] if "transaction_no" in row.keys() else None,
        "sku_id": row["sku_id"] if "sku_id" in row.keys() else None,
        "sku_name": row["sku_name"] if "sku_name" in row.keys() else None,
        "sku_name_raw": row["sku_name_raw"] if "sku_name_raw" in row.keys() else None,
        "supplier_id": row["supplier_id"] if "supplier_id" in row.keys() else None,
        "supplier_name": row["supplier_name"] if "supplier_name" in row.keys() else None,
        "lot_number": row["lot_number"] if "lot_number" in row.keys() else None,
        "vehicle_no": row["vehicle_no"] if "vehicle_no" in row.keys() else None,
        "warehouse": row["warehouse"] if "warehouse" in row.keys() else None,
        "created_at": _iso(row["created_at"]) if "created_at" in row.keys() else None,
        "readings": [],  # populated by get_detail
    }


def _reading_row_to_dict(row) -> dict:
    return {
        "reading_id": row["reading_id"],
        "parameter_id": row["parameter_id"],
        "parameter_name": row["parameter_name"],
        "parameter_unit": row["parameter_unit"],
        "observed_value_num": _f(row["observed_value_num"]),
        "observed_value_text": row["observed_value_text"],
        "spec_min": _f(row["spec_min"]),
        "spec_max": _f(row["spec_max"]),
        "spec_target": _f(row["spec_target"]),
        "is_within_spec": row["is_within_spec"],
        "severity": row["severity"],
        "deviation_pct": _f(row["deviation_pct"]),
        "method": row["method"],
        "instrument": row["instrument"],
        "notes": row["notes"],
        "recorded_at": _iso(row["recorded_at"]),
    }


# ── list_inspections ─────────────────────────────────────────────────────


async def list_inspections(
    pool,
    *,
    filters: dict,
    page: int,
    page_size: int,
) -> dict:
    """Paginated list with filter/search support."""
    conds: list[str] = []
    params: list[Any] = []
    idx = 0

    def add(sql: str, *vals: Any) -> None:
        nonlocal idx
        out = sql
        for v in vals:
            idx += 1
            out = out.replace("${i}", f"${idx}", 1)
            params.append(v)
        conds.append(out)

    # Simple equality filters
    for field in ("status", "supplier_id", "sku_id", "verdict"):
        v = filters.get(field)
        if v is not None and v != "":
            add(f"{field} = ${{i}}", v)

    # ILIKE contains on transaction_no
    txn = filters.get("transaction_no")
    if txn:
        add("transaction_no ILIKE ${i}", f"%{txn}%")

    # Date range on started_at
    from_date = filters.get("from_date")
    to_date = filters.get("to_date")
    if from_date:
        add("started_at >= ${i}::timestamptz", from_date)
    if to_date:
        add("started_at <= (${i}::date + INTERVAL '1 day')::timestamptz", to_date)

    where_sql = " AND ".join(conds) if conds else "TRUE"
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = int(
            await conn.fetchval(
                f"SELECT COUNT(*) FROM qc_inward_inspection WHERE {where_sql}",
                *params,
            ) or 0
        )
        rows = await conn.fetch(
            f"""
            SELECT inspection_id, po_number, transaction_no,
                   sku_name, sku_name_raw, sku_id, vehicle_no, warehouse,
                   verdict, status, started_at, supplier_name, lot_number,
                   inspection_ref
              FROM qc_inward_inspection
             WHERE {where_sql}
             ORDER BY started_at DESC NULLS LAST, inspection_id DESC
             LIMIT ${idx + 1} OFFSET ${idx + 2}
            """,
            *params,
            page_size,
            offset,
        )

    total_pages = math.ceil(total / page_size) if page_size > 0 else 0
    return {
        "items": [_row_to_list_item(r) for r in rows],
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
    }


# ── get_detail ────────────────────────────────────────────────────────────


async def get_detail(pool, inspection_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM qc_inward_inspection WHERE inspection_id = $1",
            inspection_id,
        )
        if row is None:
            return None
        reading_rows = await conn.fetch(
            """
            SELECT * FROM qc_inward_reading
             WHERE inspection_id = $1
             ORDER BY recorded_at, reading_id
            """,
            inspection_id,
        )
    detail = _row_to_detail(row)
    detail["readings"] = [_reading_row_to_dict(r) for r in reading_rows]
    return detail


# ── list_pending_intimations ──────────────────────────────────────────────


async def list_pending_intimations(
    pool,
    *,
    q: str | None,
    limit: int,
) -> list[dict]:
    """Pending intimations picker for the start-inspection form."""
    conds = ["status = 'pending'"]
    params: list[Any] = []
    idx = 0

    def add(sql: str, *vals: Any) -> None:
        nonlocal idx
        out = sql
        for v in vals:
            idx += 1
            out = out.replace("${i}", f"${idx}", 1)
            params.append(v)
        conds.append(out)

    if q:
        add(
            "(sku_name ILIKE ${i} OR po_number ILIKE ${i} OR transaction_no ILIKE ${i})",
            f"%{q}%",
            f"%{q}%",
            f"%{q}%",
        )

    where_sql = " AND ".join(conds)
    idx += 1
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT qc_intimation_id, sku_name, sku_name_raw, sku_id,
                   po_number, transaction_no, supplier_name, supplier_id,
                   lot_number, coa_received
              FROM qc_intimation
             WHERE {where_sql}
             ORDER BY transaction_no, created_at, qc_intimation_id
             LIMIT ${idx}
            """,
            *params,
        )
    return [
        {
            "qc_intimation_id": r["qc_intimation_id"],
            "sku_name": r["sku_name"],
            "sku_name_raw": r["sku_name_raw"] if "sku_name_raw" in r.keys() else None,
            "sku_id": r["sku_id"],
            "po_number": r["po_number"],
            "transaction_no": r["transaction_no"],
            "supplier_name": r["supplier_name"],
            "supplier_id": r["supplier_id"],
            "lot_number": r["lot_number"],
            "coa_received": bool(r["coa_received"]) if r["coa_received"] is not None else False,
        }
        for r in rows
    ]


# ── start ─────────────────────────────────────────────────────────────────


async def start(
    pool,
    *,
    qc_intimation_id: int,
    sample_size: int,
    inspection_method: str,
    remarks: str | None,
    actor_user_id: int | None,
    actor_name: str | None,
) -> int:
    """Create a new inspection from a pending intimation.

    Returns the new inspection_id.
    """
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Load + lock the intimation
            intimation = await conn.fetchrow(
                "SELECT * FROM qc_intimation WHERE qc_intimation_id = $1 FOR UPDATE",
                qc_intimation_id,
            )
            if intimation is None:
                raise AuthError(
                    "intimation_not_found",
                    f"QC intimation {qc_intimation_id} not found.",
                    404,
                )
            if intimation["status"] != "pending":
                raise AuthError(
                    "intimation_not_pending",
                    f"Intimation {qc_intimation_id} is in status "
                    f"'{intimation['status']}', expected 'pending'.",
                    409,
                )

            # INSERT inspection (inspection_ref set in a follow-up UPDATE).
            # inspection_id is an app-supplied 8-digit time-based BIGINT
            # (see app.core.helpers.new_short_time_id); PK retry handles the
            # rare same-millisecond clock collision.
            async def _insert_inspection():
                return await conn.fetchval(
                    """
                    INSERT INTO qc_inward_inspection (
                        inspection_id,
                        status, qc_intimation_id, sample_size, inspection_method,
                        started_at, started_by, started_by_name, remarks,
                        -- denormalized from intimation
                        po_number, transaction_no, sku_id, sku_name, sku_name_raw,
                        supplier_id, supplier_name, lot_number, vehicle_no, warehouse
                    )
                    VALUES (
                        $1,
                        'in_progress', $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11, $12, $13,
                        $14, $15, $16, $17, $18
                    )
                    RETURNING inspection_id
                    """,
                    new_short_time_id(),
                    qc_intimation_id,
                    sample_size,
                    inspection_method,
                    now,
                    actor_user_id,
                    actor_name,
                    remarks,
                    intimation["po_number"] if "po_number" in intimation.keys() else None,
                    intimation["transaction_no"] if "transaction_no" in intimation.keys() else None,
                    intimation["sku_id"] if "sku_id" in intimation.keys() else None,
                    intimation["sku_name"] if "sku_name" in intimation.keys() else None,
                    intimation["sku_name_raw"] if "sku_name_raw" in intimation.keys() else None,
                    intimation["supplier_id"] if "supplier_id" in intimation.keys() else None,
                    intimation["supplier_name"] if "supplier_name" in intimation.keys() else None,
                    intimation["lot_number"] if "lot_number" in intimation.keys() else None,
                    intimation["vehicle_no"] if "vehicle_no" in intimation.keys() else None,
                    intimation["warehouse"] if "warehouse" in intimation.keys() else None,
                )

            inspection_id: int = await insert_with_pk_retry(conn, _insert_inspection)

            # Set inspection_ref after we have the PK
            year = now.year
            inspection_ref = f"QC-{year}-{inspection_id:06d}"
            await conn.execute(
                "UPDATE qc_inward_inspection SET inspection_ref = $1 WHERE inspection_id = $2",
                inspection_ref,
                inspection_id,
            )

            # Lock the intimation so no duplicate inspections can be started
            await conn.execute(
                "UPDATE qc_intimation SET status = 'locked' WHERE qc_intimation_id = $1",
                qc_intimation_id,
            )

            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type="inspection_started",
                from_state=None,
                to_state="in_progress",
                actor_user_id=actor_user_id,
                payload_diff={
                    "qc_intimation_id": qc_intimation_id,
                    "inspection_ref": inspection_ref,
                    "sample_size": sample_size,
                    "inspection_method": inspection_method,
                },
            )

    return inspection_id


# ── cancel ────────────────────────────────────────────────────────────────


async def cancel(
    pool,
    *,
    inspection_id: int,
    reason: str,
    actor_user_id: int,
    actor_name: str,
) -> None:
    """Cancel an active inspection and re-queue its intimation to 'pending'."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, qc_intimation_id FROM qc_inward_inspection "
                "WHERE inspection_id = $1 FOR UPDATE",
                inspection_id,
            )
            if row is None:
                raise AuthError(
                    "inspection_not_found",
                    f"Inspection {inspection_id} not found.",
                    404,
                )
            current_status = row["status"]
            if current_status == "cancelled":
                raise AuthError(
                    "already_cancelled",
                    "Inspection is already cancelled.",
                    409,
                )

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE qc_inward_inspection SET
                    status          = 'cancelled',
                    cancelled_at    = $2,
                    cancelled_by    = $3,
                    cancelled_by_name = $4,
                    cancel_reason   = $5
                 WHERE inspection_id = $1
                """,
                inspection_id,
                now,
                actor_user_id,
                actor_name,
                reason,
            )

            # Re-queue intimation so another inspection can be started
            intimation_id = row["qc_intimation_id"]
            if intimation_id is not None:
                await conn.execute(
                    "UPDATE qc_intimation SET status = 'pending' WHERE qc_intimation_id = $1",
                    intimation_id,
                )

            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type="inspection_cancelled",
                from_state=current_status,
                to_state="cancelled",
                actor_user_id=actor_user_id,
                payload_diff={"cancel_reason": reason},
            )


# ── reopen ────────────────────────────────────────────────────────────────


async def reopen(
    pool,
    *,
    inspection_id: int,
    reason: str,
    actor_user_id: int,
    actor_name: str,
) -> None:
    """Reopen a cancelled inspection and re-bind its intimation to 'locked'."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, qc_intimation_id FROM qc_inward_inspection "
                "WHERE inspection_id = $1 FOR UPDATE",
                inspection_id,
            )
            if row is None:
                raise AuthError(
                    "inspection_not_found",
                    f"Inspection {inspection_id} not found.",
                    404,
                )
            if row["status"] != "cancelled":
                raise AuthError(
                    "not_cancelled",
                    f"Only cancelled inspections can be reopened; "
                    f"current status is '{row['status']}'.",
                    409,
                )

            now = datetime.now(timezone.utc)
            await conn.execute(
                """
                UPDATE qc_inward_inspection SET
                    status                     = 'in_progress',
                    reopened_at                = $2,
                    reopen_reason              = $3,
                    cancelled_at               = NULL,
                    cancelled_by               = NULL,
                    cancelled_by_name          = NULL,
                    cancel_reason              = NULL,
                    verdict                    = NULL,
                    verdict_at                 = NULL,
                    accepted_qty               = NULL,
                    rejected_qty               = NULL,
                    ncr_no                     = NULL,
                    verdict_overridden_by      = NULL,
                    verdict_overridden_by_name = NULL,
                    override_reason            = NULL,
                    approved_by                = NULL,
                    approved_by_name           = NULL,
                    approved_at                = NULL
                 WHERE inspection_id = $1
                """,
                inspection_id,
                now,
                reason,
            )

            # Re-bind the intimation
            intimation_id = row["qc_intimation_id"]
            if intimation_id is not None:
                await conn.execute(
                    "UPDATE qc_intimation SET status = 'locked' WHERE qc_intimation_id = $1",
                    intimation_id,
                )

            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type="inspection_reopened",
                from_state="cancelled",
                to_state="in_progress",
                actor_user_id=actor_user_id,
                payload_diff={"reopen_reason": reason},
            )


# ── update_header ─────────────────────────────────────────────────────────


async def update_header(
    pool,
    *,
    inspection_id: int,
    sample_size: int | None,
    inspection_method: str | None,
    inspector_user_id: int | None,
    remarks: str | None,
    actor_user_id: int,
) -> None:
    """Partial update of inspection header fields."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT status, sample_size, inspection_method,
                       inspector_user_id, remarks
                  FROM qc_inward_inspection
                 WHERE inspection_id = $1 FOR UPDATE
                """,
                inspection_id,
            )
            if row is None:
                raise AuthError(
                    "inspection_not_found",
                    f"Inspection {inspection_id} not found.",
                    404,
                )

            # Build payload_diff of what actually changed
            incoming = {
                "sample_size": sample_size,
                "inspection_method": inspection_method,
                "inspector_user_id": inspector_user_id,
                "remarks": remarks,
            }
            diff: dict = {}
            for field, new_val in incoming.items():
                if new_val is not None and new_val != row[field]:
                    diff[field] = {"from": row[field], "to": new_val}

            # COALESCE-style: only fields with new non-None values are updated
            await conn.execute(
                """
                UPDATE qc_inward_inspection SET
                    sample_size         = COALESCE($2, sample_size),
                    inspection_method   = COALESCE($3, inspection_method),
                    inspector_user_id   = COALESCE($4, inspector_user_id),
                    remarks             = COALESCE($5, remarks)
                 WHERE inspection_id = $1
                """,
                inspection_id,
                sample_size,
                inspection_method,
                inspector_user_id,
                remarks,
            )

            await write_audit(
                conn,
                inspection_id=inspection_id,
                event_type="inspection_updated",
                from_state=row["status"],
                to_state=row["status"],
                actor_user_id=actor_user_id,
                payload_diff=diff if diff else None,
            )
