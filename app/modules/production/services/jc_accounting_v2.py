"""Multi-stage material accounting for v2 job cards.

Backs the redesigned Accounting tab. The model:

    Stage N of M
    ────────────
    Input  = (N == 1) ? RM        : SFG carried from stage N-1
    Output = (N == M) ? FG kg/units : SFG (in this stage's UOM)

    Balance equation (per-JC, single UOM):
        total_input
          == output
           + process_loss
           + extra_give_away
           + balance_material
           + offgrade_total
           + rejection
           + wastage
           + control_sample
           + dispatched_out          (only relevant when output_kind=SFG)
           + Σ return_qty            (RM sent back to stores / prev stage)

The tab supports a mixed-UoM JC by storing each piece with its UOM and
only summing within the same unit; cross-unit conversion is out of scope
(operator-managed).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.production.services.uom import is_valid_universal, is_valid_for_kind

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tolerance — qty differences smaller than this are treated as balanced.
# Operators eyeball-weigh; ±50 g over a typical 100 kg batch is normal.
# ---------------------------------------------------------------------------
BALANCE_TOLERANCE_QTY = 0.05  # in the JC's primary UOM (kg / pcs / etc.)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _serialize(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _f(v) -> float:
    """Best-effort float coerce. None and '' both become 0."""
    if v is None or v == '':
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_accounting(conn, job_card_id: int) -> dict:
    """Return the full accounting view for a v2 JC:
        consumption rows + byproducts rows + summary + JC stage context.
    """
    jc = await conn.fetchrow(
        """
        SELECT job_card_id, plan_id, plan_line_id, step_number, process_name,
               input_kind, output_kind, uom, planned_qty_kg, planned_qty_units,
               carried_qty_kg, dispatched_to_next_kg,
               prev_job_card_id, next_job_card_id, status
        FROM   job_card_v2
        WHERE  job_card_id=$1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}

    # Total stages on the plan line (so the UI can render "Stage N of M").
    total_stages = await conn.fetchval(
        """
        SELECT COUNT(*) FROM job_card_v2
        WHERE  plan_line_id=$1 AND deleted_at IS NULL
        """,
        jc["plan_line_id"],
    )
    is_first = jc["prev_job_card_id"] is None
    is_last  = jc["next_job_card_id"] is None

    consumption = await conn.fetch(
        """
        SELECT * FROM job_card_material_consumption_v2
        WHERE  job_card_id=$1
        ORDER  BY consumption_id
        """,
        job_card_id,
    )
    byproducts = await conn.fetch(
        """
        SELECT * FROM job_card_byproducts_v2
        WHERE  job_card_id=$1
        ORDER  BY byproduct_id
        """,
        job_card_id,
    )
    accounting = await conn.fetchrow(
        "SELECT * FROM job_card_accounting_v2 WHERE job_card_id=$1",
        job_card_id,
    )

    return {
        "job_card_id":   jc["job_card_id"],
        "stage": {
            "step_number":   jc["step_number"],
            "process_name":  jc["process_name"],
            "input_kind":    jc["input_kind"],
            "output_kind":   jc["output_kind"],
            "is_first_stage": is_first,
            "is_last_stage":  is_last,
            "total_stages":   total_stages,
            "prev_job_card_id": jc["prev_job_card_id"],
            "next_job_card_id": jc["next_job_card_id"],
            "planned_qty_kg":    float(jc["planned_qty_kg"]) if jc["planned_qty_kg"] is not None else None,
            "planned_qty_units": float(jc["planned_qty_units"]) if jc["planned_qty_units"] is not None else None,
            "uom":               jc["uom"],
            "carried_in_qty":    float(jc["carried_qty_kg"] or 0),
            "dispatched_out_qty": float(jc["dispatched_to_next_kg"] or 0),
        },
        "consumption": [_serialize(r) for r in consumption],
        "byproducts":  [_serialize(r) for r in byproducts],
        "accounting":  _serialize(accounting) if accounting else None,
    }


# ---------------------------------------------------------------------------
# Save consumption rows (upsert per material)
# ---------------------------------------------------------------------------

async def save_consumption(conn, *, job_card_id: int,
                           rows: list[dict],
                           recorded_by: str | None = None) -> dict:
    """Upsert consumption rows for this JC. Caller supplies a list of:
        { material_sku_name, input_kind, uom, issued_qty,
          actual_consumed_qty, return_qty?, remarks?,
          source_rm_indent_id?, source_dispatch_id? }
    Rows are matched on (job_card_id, material_sku_name) via the table's
    UNIQUE index — duplicates update in place.
    """
    saved: list[dict] = []
    for r in rows:
        material = (r.get("material_sku_name") or "").strip()
        if not material:
            continue
        input_kind = r.get("input_kind") or "RM"
        uom        = (r.get("uom") or "KGS").upper()

        if not is_valid_universal(uom):
            return {"error": "invalid_uom", "uom": uom}
        if input_kind not in ('RM', 'SFG', 'WIP', 'PM'):
            return {"error": "invalid_input_kind", "input_kind": input_kind}

        issued   = _f(r.get("issued_qty"))
        actual   = _f(r.get("actual_consumed_qty"))
        ret      = _f(r.get("return_qty"))
        variance = round(actual - issued, 3)

        # consumption_id is app-supplied (migration 019). On the common
        # path the (job_card_id, material_sku_name) UNIQUE triggers an
        # UPDATE which preserves the existing PK — our candidate is just
        # ignored. On a true new insert we attempt the candidate ID; a
        # PK collision (rare) triggers retry via insert_with_pk_retry.
        async def _insert(
            _job_card_id=job_card_id, _material=material, _input_kind=input_kind,
            _uom=uom, _issued=issued, _actual=actual, _ret=ret,
            _variance=variance, _src_rm=r.get("source_rm_indent_id"),
            _src_dispatch=r.get("source_dispatch_id"),
            _remarks=r.get("remarks"), _recorded_by=recorded_by,
        ):
            return await conn.fetchrow(
                """
                INSERT INTO job_card_material_consumption_v2
                    (consumption_id, job_card_id, material_sku_name, input_kind, uom,
                     issued_qty, actual_consumed_qty, return_qty, variance,
                     source_rm_indent_id, source_dispatch_id,
                     remarks, recorded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (job_card_id, material_sku_name) DO UPDATE SET
                    input_kind          = EXCLUDED.input_kind,
                    uom                 = EXCLUDED.uom,
                    issued_qty          = EXCLUDED.issued_qty,
                    actual_consumed_qty = EXCLUDED.actual_consumed_qty,
                    return_qty          = EXCLUDED.return_qty,
                    variance            = EXCLUDED.variance,
                    source_rm_indent_id = EXCLUDED.source_rm_indent_id,
                    source_dispatch_id  = EXCLUDED.source_dispatch_id,
                    remarks             = EXCLUDED.remarks,
                    recorded_by         = EXCLUDED.recorded_by,
                    recorded_at         = NOW()
                RETURNING *
                """,
                new_short_time_id(),
                _job_card_id, _material, _input_kind, _uom,
                _issued, _actual, _ret, _variance,
                _src_rm, _src_dispatch, _remarks, _recorded_by,
            )
        row = await insert_with_pk_retry(conn, _insert)
        saved.append(_serialize(row))
    return {"saved": True, "rows": saved}


# ---------------------------------------------------------------------------
# Save byproduct rows (upsert per category)
# ---------------------------------------------------------------------------

VALID_BP_CATEGORIES = (
    'tukda', 'damaged', 'black_stained', 'without_shell', 'empty_shells',
    'dust', 'balance_material', 'rejection', 'control_sample', 'other',
)


async def save_byproducts(conn, *, job_card_id: int,
                          rows: list[dict],
                          recorded_by: str | None = None) -> dict:
    """Upsert byproduct rows. Zero-qty rows are allowed (the UI can clear
    a previously-saved category by setting it to 0)."""
    saved: list[dict] = []
    for r in rows:
        cat = r.get("category")
        if cat not in VALID_BP_CATEGORIES:
            return {"error": "invalid_category", "category": cat}
        uom = (r.get("uom") or "KGS").upper()
        if not is_valid_universal(uom):
            return {"error": "invalid_uom", "uom": uom}
        qty = _f(r.get("quantity"))
        if qty < 0:
            return {"error": "negative_qty", "category": cat}

        async def _insert(
            _job_card_id=job_card_id, _cat=cat, _qty=qty, _uom=uom,
            _remarks=r.get("remarks"), _recorded_by=recorded_by,
        ):
            return await conn.fetchrow(
                """
                INSERT INTO job_card_byproducts_v2
                    (byproduct_id, job_card_id, category, quantity, uom, remarks, recorded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (job_card_id, category) DO UPDATE SET
                    quantity    = EXCLUDED.quantity,
                    uom         = EXCLUDED.uom,
                    remarks     = EXCLUDED.remarks,
                    recorded_by = EXCLUDED.recorded_by,
                    recorded_at = NOW()
                RETURNING *
                """,
                new_short_time_id(),
                _job_card_id, _cat, _qty, _uom, _remarks, _recorded_by,
            )
        row = await insert_with_pk_retry(conn, _insert)
        saved.append(_serialize(row))
    return {"saved": True, "rows": saved}


# ---------------------------------------------------------------------------
# Save accounting summary + balance check
# ---------------------------------------------------------------------------

async def save_accounting(conn, *, job_card_id: int,
                          payload: dict,
                          saved_by: str | None = None) -> dict:
    """Save the summary row and compute is_balanced + percentages.

    `payload` shape:
        {
          "total_input_qty": float,
          "input_uom":  str,
          "output_qty": float,
          "output_uom": str,
          "output_qty_units": float | null,    # only on last stage (FG)
          "process_loss_qty": float,
          "process_loss_breakdown": {moisture_loss, roasting_loss, ...},
          "extra_give_away_qty": float,
          "balance_material_qty": float,
          "offgrade_total_qty": float,
          "rejection_qty": float,
          "wastage_qty": float,
          "control_sample_qty": float,
        }

    Numbers come from the UI; the byproducts / process-loss-detail tables
    are the source of truth — the UI rolls them up before posting here.
    """
    jc = await conn.fetchrow(
        """
        SELECT job_card_id, output_kind, uom, carried_qty_kg, dispatched_to_next_kg
        FROM   job_card_v2
        WHERE  job_card_id=$1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}

    input_uom  = (payload.get("input_uom")  or jc["uom"] or "KGS").upper()
    output_uom = (payload.get("output_uom") or jc["uom"] or "KGS").upper()
    output_kind = jc["output_kind"]

    if not is_valid_universal(input_uom) or not is_valid_universal(output_uom):
        return {"error": "invalid_uom",
                "input_uom": input_uom, "output_uom": output_uom}

    total_input  = _f(payload.get("total_input_qty"))
    output_qty   = _f(payload.get("output_qty"))
    output_units = payload.get("output_qty_units")
    output_units = _f(output_units) if output_units not in (None, "") else None

    process_loss   = _f(payload.get("process_loss_qty"))
    breakdown_raw  = payload.get("process_loss_breakdown") or {}
    # Coerce all sub-values to floats so the JSONB stays well-typed.
    process_loss_breakdown = {k: _f(v) for k, v in breakdown_raw.items()}

    extra_give     = _f(payload.get("extra_give_away_qty"))
    balance_mat    = _f(payload.get("balance_material_qty"))
    offgrade       = _f(payload.get("offgrade_total_qty"))
    rejection      = _f(payload.get("rejection_qty"))
    wastage        = _f(payload.get("wastage_qty"))
    control_sample = _f(payload.get("control_sample_qty"))

    carried_in     = _f(jc["carried_qty_kg"])
    dispatched_out = _f(jc["dispatched_to_next_kg"])

    # ── Balance equation (single UOM at the JC level) ──
    # Sum total return_qty off consumption rows — RM that went back to
    # stores / prev stage is part of what we accounted for.
    total_return = _f(await conn.fetchval(
        "SELECT COALESCE(SUM(return_qty), 0) FROM job_card_material_consumption_v2 WHERE job_card_id=$1",
        job_card_id,
    ))

    total_accounted = (
        output_qty + process_loss + extra_give + balance_mat
        + offgrade + rejection + wastage + control_sample
        + dispatched_out + total_return
    )
    diff = round(total_input - total_accounted, 3)
    is_balanced = abs(diff) <= BALANCE_TOLERANCE_QTY

    # ── Loss percentages (relative to total input) ──
    if total_input > 0:
        process_loss_pct = round((process_loss / total_input) * 100, 3)
        other_losses     = offgrade + rejection + wastage
        other_loss_pct   = round((other_losses / total_input) * 100, 3)
        total_loss_pct   = round(((process_loss + other_losses) / total_input) * 100, 3)
    else:
        process_loss_pct = other_loss_pct = total_loss_pct = None

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_accounting_v2 (
                accounting_id,
                job_card_id,
                total_input_qty, input_uom,
                output_qty, output_uom, output_qty_units, output_kind,
                carried_in_qty, dispatched_out_qty,
                process_loss_qty, process_loss_breakdown,
                extra_give_away_qty, balance_material_qty,
                offgrade_total_qty, rejection_qty, wastage_qty, control_sample_qty,
                total_accounted_qty, balance_difference_qty, is_balanced,
                process_loss_pct, other_loss_pct, total_loss_pct,
                saved_by
            ) VALUES (
                $1,
                $2,
                $3, $4,
                $5, $6, $7, $8,
                $9, $10,
                $11, $12::jsonb,
                $13, $14,
                $15, $16, $17, $18,
                $19, $20, $21,
                $22, $23, $24,
                $25
            )
            ON CONFLICT (job_card_id) DO UPDATE SET
                total_input_qty        = EXCLUDED.total_input_qty,
                input_uom              = EXCLUDED.input_uom,
                output_qty             = EXCLUDED.output_qty,
                output_uom             = EXCLUDED.output_uom,
                output_qty_units       = EXCLUDED.output_qty_units,
                output_kind            = EXCLUDED.output_kind,
                carried_in_qty         = EXCLUDED.carried_in_qty,
                dispatched_out_qty     = EXCLUDED.dispatched_out_qty,
                process_loss_qty       = EXCLUDED.process_loss_qty,
                process_loss_breakdown = EXCLUDED.process_loss_breakdown,
                extra_give_away_qty    = EXCLUDED.extra_give_away_qty,
                balance_material_qty   = EXCLUDED.balance_material_qty,
                offgrade_total_qty     = EXCLUDED.offgrade_total_qty,
                rejection_qty          = EXCLUDED.rejection_qty,
                wastage_qty            = EXCLUDED.wastage_qty,
                control_sample_qty     = EXCLUDED.control_sample_qty,
                total_accounted_qty    = EXCLUDED.total_accounted_qty,
                balance_difference_qty = EXCLUDED.balance_difference_qty,
                is_balanced            = EXCLUDED.is_balanced,
                process_loss_pct       = EXCLUDED.process_loss_pct,
                other_loss_pct         = EXCLUDED.other_loss_pct,
                total_loss_pct         = EXCLUDED.total_loss_pct,
                saved_by               = EXCLUDED.saved_by,
                saved_at               = NOW()
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id,
            total_input, input_uom,
            output_qty, output_uom, output_units, output_kind,
            carried_in, dispatched_out,
            process_loss, json.dumps(process_loss_breakdown),
            extra_give, balance_mat,
            offgrade, rejection, wastage, control_sample,
            total_accounted, diff, is_balanced,
            process_loss_pct, other_loss_pct, total_loss_pct,
            saved_by,
        )
    row = await insert_with_pk_retry(conn, _insert)
    return {
        "saved": True,
        "accounting": _serialize(row),
        "total_accounted_qty": total_accounted,
        "balance_difference_qty": diff,
        "is_balanced": is_balanced,
    }
