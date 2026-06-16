"""Multi-stage material accounting for v2 job cards.

Backs the redesigned Accounting tab. The model:

    Stage N of M
    ────────────
    Input  = (N == 1) ? RM        : SFG carried from stage N-1
    Output = (N == M) ? FG kg/units : SFG (in this stage's UOM)

Convention (load-bearing - do not violate without re-reading the balance
equation below):
  * `total_input_qty` is the COMPREHENSIVE input mass for the stage.
    For stage 1 it's the RM consumed. For stage 2+ it IS the SFG carried
    in from the previous stage. The separate `carried_in_qty` column on
    job_card_accounting_v2 mirrors `job_card_v2.carried_qty_kg` and exists
    as an AUDIT field - it is NOT a separate addend to the balance.
  * `output_qty` is mass still on the JC's floor (production retained).
    `dispatched_out_qty` is the moved portion. They PARTITION the
    production - never overlap. The balance equation sums both because
    together they cover everything physically produced.

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

from asyncpg.exceptions import UndefinedColumnError

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.production.services.job_card_v2 import (
    assert_not_locked,
    resolve_bom_multiplier,
)
from app.modules.production.services.uom import is_valid_universal, is_valid_for_kind


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# B12 — Consumption variance background capture
#
# After every accounting/consumption or accounting/summary save, recompute
# the per-material BOM-vs-actual variance and upsert it into
# job_card_consumption_variance_v2 (table from migration 028). Cost columns
# stay NULL until the future commercial / inventory-costing workstream
# wires them up; the qty data accumulates silently from day one.
#
# The variance row carries SNAPSHOTS of bom_version + bom_prescribed_qty
# so future BOM amendments do not retroactively shift historical reports.
# A material in consumption that isn't in the BOM (e.g. an R8 row-1 one-off
# addition) is recorded with bom_prescribed_qty=0, variance_qty=actual,
# variance_pct=NULL.
# ---------------------------------------------------------------------------

async def _record_consumption_variance(conn, job_card_id: int) -> dict:
    """Compute per-row variance for this JC's consumption and upsert.

    Formula (load-bearing — see job_card_v2.py:224-227 / :263 for the
    canonical convention):

        bom_line.quantity_per_unit is the BOM convention 'per kg of FG'
        (matches v1's job_card_engine math), so reqd = planned_qty_kg ×
        quantity_per_unit.

    Scope: only RM consumption rows are tracked here. SFG/WIP carry-ins
    (synthetic material names like 'SFG from JC #...') and PM rows are
    excluded — variance only makes sense for raw materials. PM variance
    is captured separately in job_card_accounting_v2.pm_variance_breakdown
    JSONB (R11, owned by save_byproducts).

    UoM reconciliation: if consumption.uom != bom_line.uom
    (case-insensitive) the row is added to skipped[] with
    reason='uom_mismatch' and NO variance row is inserted. Cost math is
    deferred to the future commercial workstream — this capture is
    gate-only, so we refuse to subtract 500 GMS from 0.5 KGS rather than
    attempt canonicalization here.

    Returns a small summary {"rows": N, "skipped": [...]}. Failures are
    logged but never raised - this is best-effort capture and must NOT
    fail the operational save.
    """
    summary: dict = {"rows": 0, "skipped": []}

    jc = await conn.fetchrow(
        "SELECT bom_id, planned_qty_kg, planned_qty_units, fg_sku_name "
        "FROM   job_card_v2 WHERE job_card_id=$1 AND deleted_at IS NULL",
        job_card_id,
    )
    if jc is None:
        summary["skipped"].append("job_card_not_found")
        return summary

    bom_id = jc["bom_id"]
    bom_version: int | None = None
    if bom_id is not None:
        bom_version = await conn.fetchval(
            "SELECT version FROM bom_header WHERE bom_id=$1", bom_id,
        )

    # Pull RM consumption rows for this JC. SFG/WIP carry-ins and PM rows
    # are intentionally excluded — variance only makes sense for raw
    # materials; PM variance lives in pm_variance_breakdown JSONB (R11).
    # The variance table tracks exactly these RM rows 1:1
    # (material_sku_name is the join key).
    consumption_rows = await conn.fetch(
        """
        SELECT material_sku_name, actual_consumed_qty, uom, input_kind
        FROM   job_card_material_consumption_v2
        WHERE  job_card_id=$1 AND input_kind='RM'
        """,
        job_card_id,
    )

    # Note: non-RM consumption rows (SFG/WIP carry-ins, PM) are filtered
    # out at the SQL level above. They are NOT errors — they're simply
    # out of scope for material variance tracking. If the operator wants
    # to see them in the skipped list, surface the count here for audit.
    excluded_non_rm = await conn.fetchval(
        """
        SELECT COUNT(*) FROM job_card_material_consumption_v2
        WHERE  job_card_id=$1 AND input_kind <> 'RM'
        """,
        job_card_id,
    )
    if excluded_non_rm:
        summary["skipped"].append({
            "reason": "non_rm_rows_excluded",
            "count":  int(excluded_non_rm),
            "note":   "SFG/WIP/PM rows are out of scope; PM variance is in "
                      "job_card_accounting_v2.pm_variance_breakdown (R11).",
        })

    # Resolve the BOM multiplier ONCE per JC. Variance is meaningful
    # against ACTUAL FG output, not planned — given the actual FG you
    # produced, BOM says you should have consumed Y, you used Z, real
    # variance = (Z - Y) / Y. Planned qty is only the fallback when no
    # output rows exist yet (fresh JC before first save).
    #
    # The uom-basis decision (units vs kg) is unchanged — see
    # job_card_v2.resolve_bom_multiplier.
    fg_actual = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(output_qty_kg),    0) AS actual_kg,
               COALESCE(SUM(output_qty_units), 0) AS actual_units
        FROM   job_card_output_v2
        WHERE  job_card_id = $1
        """,
        job_card_id,
    )
    actual_kg    = float(fg_actual["actual_kg"]    or 0) if fg_actual else 0.0
    actual_units = float(fg_actual["actual_units"] or 0) if fg_actual else 0.0
    eff_kg    = actual_kg    if actual_kg    > 0 else float(jc["planned_qty_kg"] or 0)
    eff_units = actual_units if actual_units > 0 else (
        float(jc["planned_qty_units"]) if jc["planned_qty_units"] is not None else None
    )
    multiplier, basis, sku_uom = await resolve_bom_multiplier(
        conn,
        fg_sku_name=jc["fg_sku_name"],
        qty_kg=eff_kg,
        qty_units=eff_units,
    )

    for crow in consumption_rows:
        material = crow["material_sku_name"]
        actual   = _f(crow["actual_consumed_qty"])
        uom      = (crow["uom"] or "KGS").upper()

        bom_prescribed_qty = 0.0
        bom_uom: str | None = None
        # Look up the BOM line for this material. If multiple lines share
        # the same material_sku_name (rare), pick the lowest line_number
        # for determinism (matches B6 H2/H3 fix elsewhere).
        if bom_id is not None:
            bom_row = await conn.fetchrow(
                "SELECT quantity_per_unit, uom FROM bom_line "
                "WHERE  bom_id=$1 AND material_sku_name=$2 "
                "ORDER  BY line_number LIMIT 1",
                bom_id, material,
            )
            if bom_row is not None:
                qty_per_unit = _f(bom_row["quantity_per_unit"])
                # Multiplier basis depends on all_sku.uom (see
                # resolve_bom_multiplier). For per-piece FG SKUs
                # (uom != 1) it's planned_qty_units; for uom == 1 or NULL
                # it's planned_qty_kg.
                bom_prescribed_qty = qty_per_unit * multiplier
                bom_uom = (bom_row["uom"] or "").upper() or None

        # H2 UoM gate — if the consumption row's UoM doesn't match the
        # BOM line's UoM, refuse to compute variance. The framework
        # defers cost math; subtracting 500 GMS from 0.5 KGS would
        # produce nonsense. Skip the row entirely (no insert) and
        # surface it on the skipped[] audit list. When there's no BOM
        # line (bom_uom is None), the row is treated as an off-BOM
        # one-off and falls through to the normal insert path with
        # bom_prescribed_qty=0.
        if bom_uom is not None and bom_uom != uom:
            summary["skipped"].append({
                "material": material,
                "reason":   "uom_mismatch",
                "consumption_uom": uom,
                "bom_uom":         bom_uom,
            })
            continue

        async def _insert(
            _material=material, _actual=actual, _uom=uom,
            _bom_prescribed_qty=bom_prescribed_qty,
            _bom_version=bom_version, _bom_id=bom_id,
        ):
            # variance_qty and variance_pct are GENERATED ALWAYS in the
            # table (migration 030), so we don't supply them.
            return await conn.fetchrow(
                """
                INSERT INTO job_card_consumption_variance_v2 (
                    variance_id, job_card_id, material_sku_name,
                    bom_id, bom_version, bom_prescribed_qty,
                    actual_consumed_qty, uom
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (job_card_id, material_sku_name) DO UPDATE SET
                    bom_id             = EXCLUDED.bom_id,
                    bom_version        = EXCLUDED.bom_version,
                    bom_prescribed_qty = EXCLUDED.bom_prescribed_qty,
                    actual_consumed_qty = EXCLUDED.actual_consumed_qty,
                    uom                = EXCLUDED.uom,
                    last_updated_at    = NOW()
                RETURNING variance_id
                """,
                new_short_time_id(),
                job_card_id, _material,
                _bom_id, _bom_version,
                _bom_prescribed_qty, _actual, _uom,
            )

        try:
            await insert_with_pk_retry(conn, _insert)
            summary["rows"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "variance capture failed for jc=%s material=%s: %s",
                job_card_id, material, exc,
            )
            summary["skipped"].append({
                "material": material,
                "reason":   f"{type(exc).__name__}: {exc}",
            })

    return summary


# ---------------------------------------------------------------------------
# Tolerance — qty differences smaller than this are treated as balanced.
# Operators eyeball-weigh; ±50 g over a typical 100 kg batch is normal.
#
# Two regimes (R9):
#   * Normal path: percentage tolerance pulled from bom_header
#     (allowed_balance_tolerance_pct, migration 028). Default 0.001 (0.1%).
#   * Fallback path: when total_input + carried_in == 0 we can't divide,
#     so the absolute-qty tolerance below kicks in.
# ---------------------------------------------------------------------------
BALANCE_TOLERANCE_QTY     = 0.05    # absolute fallback when no input
BALANCE_TOLERANCE_PCT_DEF = 0.001   # 0.1% — matches bom_header default


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

async def get_accounting(conn, job_card_id: int, *, batch_id: int | None = None) -> dict:
    """Return the full accounting view for a v2 JC:
        consumption rows + byproducts rows + summary + JC stage context.

    When ``batch_id`` is supplied, ``consumption`` and ``byproducts`` are
    restricted to that batch (rows still carrying NULL batch_id are also
    included — they're legacy / record_output-pre-batch_id_fix rows that
    should still surface under the current batch instead of vanishing).
    The ``accounting`` summary row is JC-level (one per JC); it's returned
    unchanged. ``consumption_variance`` is also JC-level by design — it
    aggregates BOM vs actual across the whole JC.
    """
    jc = await conn.fetchrow(
        """
        SELECT job_card_id, plan_id, plan_line_id, step_number, process_name,
               input_kind, output_kind, uom, planned_qty_kg, planned_qty_units,
               carried_qty_kg, dispatched_to_next_kg,
               prev_job_card_id, next_job_card_id, status,
               bom_id
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

    # batch_id filter: NULL → include all (legacy + every batch).
    # int → match that batch_id OR rows still carrying NULL (defensive
    # parity with the frontend's matchesBatch helper, which keeps
    # NULL-batch rows visible under the currently picked batch so legacy
    # data doesn't silently vanish from the operator's view).
    #
    # Pre-migration-038 environments don't have a batch_id column on the
    # accounting tables. Catch UndefinedColumnError and fall back to the
    # unfiltered query (per-batch filtering is impossible on that schema
    # anyway — the caller will see the same JC-wide rows the old endpoint
    # returned). Mirrors the fallback pattern in job_card_v2.get_job_card.
    if batch_id is None:
        consumption = await conn.fetch(
            """
            SELECT * FROM job_card_material_consumption_v2
            WHERE  job_card_id=$1
            ORDER  BY consumption_id
            """,
            job_card_id,
        )
    else:
        try:
            consumption = await conn.fetch(
                """
                SELECT * FROM job_card_material_consumption_v2
                WHERE  job_card_id=$1
                  AND  (batch_id = $2 OR batch_id IS NULL)
                ORDER  BY consumption_id
                """,
                job_card_id, batch_id,
            )
        except UndefinedColumnError:
            logger.warning(
                "get_accounting: job_card_material_consumption_v2.batch_id "
                "missing — falling back to unfiltered SELECT. Apply migration 038."
            )
            consumption = await conn.fetch(
                """
                SELECT * FROM job_card_material_consumption_v2
                WHERE  job_card_id=$1
                ORDER  BY consumption_id
                """,
                job_card_id,
            )
    # fe-H1 / web-C3-CRIT-2: surface every persisted BOM-vs-actual variance
    # row so the Accounting tab can render the variance table without a
    # separate round-trip. Cost columns on this table (unit_cost_at_consumption,
    # variance_cost_impact, cost_basis) are part of COST_BEARING_FIELDS and the
    # response_filters.strip_cost_fields() walker drops them for deny-list
    # roles before the payload leaves the API layer (B13).
    consumption_variance = await conn.fetch(
        """
        SELECT material_sku_name, bom_prescribed_qty, actual_consumed_qty,
               variance_qty, variance_pct, uom, bom_version, last_updated_at,
               unit_cost_at_consumption, variance_cost_impact, cost_basis
        FROM   job_card_consumption_variance_v2
        WHERE  job_card_id=$1
        ORDER  BY material_sku_name
        """,
        job_card_id,
    )
    if batch_id is None:
        byproducts = await conn.fetch(
            """
            SELECT * FROM job_card_byproducts_v2
            WHERE  job_card_id=$1
            ORDER  BY byproduct_id
            """,
            job_card_id,
        )
    else:
        try:
            byproducts = await conn.fetch(
                """
                SELECT * FROM job_card_byproducts_v2
                WHERE  job_card_id=$1
                  AND  (batch_id = $2 OR batch_id IS NULL)
                ORDER  BY byproduct_id
                """,
                job_card_id, batch_id,
            )
        except UndefinedColumnError:
            logger.warning(
                "get_accounting: job_card_byproducts_v2.batch_id "
                "missing — falling back to unfiltered SELECT. Apply migration 038."
            )
            byproducts = await conn.fetch(
                """
                SELECT * FROM job_card_byproducts_v2
                WHERE  job_card_id=$1
                ORDER  BY byproduct_id
                """,
                job_card_id,
            )
    # Migration 049: job_card_accounting_v2 became per-(JC, batch). When
    # the caller asked for a specific batch, return that batch's row. The
    # COALESCE-0 sentinel matches the legacy "one row per JC" path that
    # older callers (no batch_id passed to the PUT) wrote into. Pre-049
    # databases that don't have the batch_id column raise
    # UndefinedColumnError → fall back to the JC-level fetch so this
    # endpoint stays loadable until psql runs.
    if batch_id is None:
        # No batch filter: return the most recently saved row. Multiple
        # rows exist post-049 (one per batch); picking the latest is the
        # closest match to the old "single JC row" contract that historic
        # callers expected. The accounting object on the JC detail GET
        # uses the SUM/BOOL_AND roll-up instead — this endpoint stays a
        # single-row contract for back-compat.
        accounting = await conn.fetchrow(
            "SELECT * FROM job_card_accounting_v2 WHERE job_card_id=$1 "
            "ORDER BY saved_at DESC LIMIT 1",
            job_card_id,
        )
    else:
        try:
            accounting = await conn.fetchrow(
                """
                SELECT * FROM job_card_accounting_v2
                WHERE  job_card_id = $1
                  AND  COALESCE(batch_id, 0) = COALESCE($2, 0)
                """,
                job_card_id, batch_id,
            )
        except UndefinedColumnError:
            logger.warning(
                "get_accounting: job_card_accounting_v2.batch_id "
                "missing — falling back to JC-level SELECT. Apply migration 049."
            )
            accounting = await conn.fetchrow(
                "SELECT * FROM job_card_accounting_v2 WHERE job_card_id=$1",
                job_card_id,
            )

    # rejection_pct / offgrade_pct now anchor on FG actual output_qty
    # instead of total_input_qty — matches the operator-stated rule
    # that loss percentages should reflect "share of FG produced".
    # rejection_pct is still emitted so any legacy reader doesn't 500,
    # but the UI no longer surfaces it (off-grade and rejection are
    # one operational bucket per the same policy).
    accounting_payload = _serialize(accounting) if accounting else None
    if accounting_payload is not None:
        out_qty = _f(accounting_payload.get("output_qty"))
        if out_qty > 0:
            rejection_qty = _f(accounting_payload.get("rejection_qty"))
            offgrade_qty  = _f(accounting_payload.get("offgrade_total_qty"))
            accounting_payload["rejection_pct"] = round(
                (rejection_qty / out_qty) * 100, 3,
            )
            accounting_payload["offgrade_pct"] = round(
                (offgrade_qty / out_qty) * 100, 3,
            )
        else:
            accounting_payload["rejection_pct"] = None
            accounting_payload["offgrade_pct"]  = None

        tolerance_pct = None
        if jc["bom_id"] is not None:
            tolerance_pct = await conn.fetchval(
                "SELECT allowed_balance_tolerance_pct FROM bom_header WHERE bom_id=$1",
                jc["bom_id"],
            )
        if tolerance_pct is None:
            tolerance_pct = BALANCE_TOLERANCE_PCT_DEF
        accounting_payload["allowed_balance_tolerance_pct"] = float(tolerance_pct)

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
        "consumption":          [_serialize(r) for r in consumption],
        "consumption_variance": [_serialize(r) for r in consumption_variance],
        "byproducts":           [_serialize(r) for r in byproducts],
        "accounting":           accounting_payload,
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
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
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

    # B12: silent variance capture after the consumption upsert.
    try:
        variance_summary = await _record_consumption_variance(conn, job_card_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("variance capture wrapper failed: %s", exc)
        variance_summary = {"rows": 0, "skipped": [f"wrapper:{exc!r}"]}
    return {"saved": True, "rows": saved, "variance": variance_summary}


# ---------------------------------------------------------------------------
# Save byproduct rows (upsert per category)
# ---------------------------------------------------------------------------

VALID_BP_CATEGORIES = (
    'tukda', 'damaged', 'black_stained', 'without_shell', 'empty_shells',
    'dust', 'balance_material', 'rejection', 'control_sample',
    # Migration 047 promoted 'wastage' to a first-class category — the
    # frontend's Off-Grade dropdown has had it for ages with a comment
    # claiming it was already allowed server-side, but neither this
    # tuple nor the DB CHECK constraint included it, so every save with
    # a Wastage off-grade row 400'd with invalid_category.
    'wastage',
    'other',
    # R11 PM variance buckets - migration 028 extends the DB CHECK enum.
    'pm_torn', 'pm_damaged', 'pm_misprint', 'pm_rejection', 'pm_wasted',
)

# R11 PM-variance categories: must be saved with a piece-denominated UOM
# (not mass). See save_byproducts() for the UOM gate.
PM_VARIANCE_CATEGORIES = frozenset({
    'pm_torn', 'pm_damaged', 'pm_misprint', 'pm_rejection', 'pm_wasted',
})
PM_VALID_UOMS = frozenset({'PCS', 'NOS', 'ROLL', 'SETS', 'BUNDLE'})


async def save_byproducts(conn, *, job_card_id: int,
                          rows: list[dict],
                          recorded_by: str | None = None,
                          batch_id: int | None = None) -> dict:
    """Upsert byproduct rows. Zero-qty rows are allowed (the UI can clear
    a previously-saved category by setting it to 0).

    R11 PM variance validation: the pm_torn / pm_damaged / pm_misprint /
    pm_rejection / pm_wasted categories (added in migration 028) must be
    in a piece-denominated UOM. Mass UOMs (KGS / GMS / LTRS) would let an
    operator silently roll a 50-pcs label rejection into a 50-kg loss,
    crushing the conservation math."""
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err

    # Migration-034 cleanup: legacy rows saved BEFORE the material
    # attribution columns existed have material_name IS NULL. After 034
    # the unique key became (jc, category, COALESCE(material_name, '')),
    # so the next save WITH an article picked produces a SECOND row
    # instead of replacing the legacy NULL one — operator sees the
    # category twice in the UI (once as "— Article —", once with the
    # picked article).
    #
    # Strategy: before the per-row upsert, if the incoming payload
    # contains at least one attributed (non-empty material_name) row
    # for a category, delete any legacy NULL-material row for that
    # same (jc, category). The NULL row only existed because the
    # system couldn't store attribution at save time; once attribution
    # arrives it is the canonical truth for that (jc, category).
    #
    # Categories that genuinely don't carry an article (control_sample,
    # pm_*, dust as a whole-batch sweep) are NOT affected — their
    # incoming rows pass NULL too, so this set excludes them.
    attributed_cats = sorted({
        r.get("category") for r in rows
        if r.get("category")
        and isinstance(r.get("material_name"), str)
        and r["material_name"].strip()
    })
    if attributed_cats:
        await conn.execute(
            """
            DELETE FROM job_card_byproducts_v2
            WHERE  job_card_id = $1
              AND  material_name IS NULL
              AND  category = ANY($2::text[])
            """,
            job_card_id, attributed_cats,
        )

    saved: list[dict] = []
    for r in rows:
        cat = r.get("category")
        if cat not in VALID_BP_CATEGORIES:
            return {"error": "invalid_category", "category": cat}
        uom = (r.get("uom") or "KGS").upper()
        if not is_valid_universal(uom):
            return {"error": "invalid_uom", "uom": uom}
        # R11: PM-variance categories must be piece-denominated.
        if cat in PM_VARIANCE_CATEGORIES and uom not in PM_VALID_UOMS:
            return {
                "error": "pm_category_invalid_uom",
                "category": cat,
                "uom": uom,
                "message": (
                    f"PM-variance category '{cat}' requires a piece UOM "
                    f"({sorted(PM_VALID_UOMS)}); got '{uom}'."
                ),
            }
        qty = _f(r.get("quantity"))
        if qty < 0:
            return {"error": "negative_qty", "category": cat}

        # Migration 034 added material attribution. material_name + bom_line_id
        # are optional — control_sample, pm_*, dust etc. don't attribute to an
        # article and pass NULL. Off-grade / rejection rows DO carry an article;
        # the unique key (jc, category, COALESCE(material_name, '')) lets
        # multiple rejection rows coexist as long as they target distinct
        # materials.
        material_name = r.get("material_name")
        if isinstance(material_name, str):
            material_name = material_name.strip() or None
        bom_line_id = r.get("bom_line_id")
        try:
            bom_line_id = int(bom_line_id) if bom_line_id not in (None, "") else None
        except (TypeError, ValueError):
            bom_line_id = None

        async def _insert(
            _job_card_id=job_card_id, _cat=cat, _qty=qty, _uom=uom,
            _remarks=r.get("remarks"), _recorded_by=recorded_by,
            _material_name=material_name, _bom_line_id=bom_line_id,
            _batch_id=batch_id,
        ):
            # Stage 2: UNIQUE key extended to include batch_id (migration
            # 038 dropped uq_byproducts_jc_cat_mat and created
            # uq_byproducts_jc_batch_cat_mat).  ON CONFLICT lists the
            # full key expression so PG matches the index automatically.
            return await conn.fetchrow(
                """
                INSERT INTO job_card_byproducts_v2
                    (byproduct_id, job_card_id, batch_id, category, quantity,
                     uom, remarks, recorded_by, material_name, bom_line_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (job_card_id, COALESCE(batch_id, 0),
                             category, (COALESCE(material_name, '')))
                DO UPDATE SET
                    quantity    = EXCLUDED.quantity,
                    uom         = EXCLUDED.uom,
                    remarks     = EXCLUDED.remarks,
                    recorded_by = EXCLUDED.recorded_by,
                    bom_line_id = EXCLUDED.bom_line_id,
                    -- Promote batch_id so a save against batch X always
                    -- leaves the row tagged with X. The ON CONFLICT key
                    -- uses COALESCE(batch_id, 0) and can match a legacy
                    -- NULL-batch row; without this, the DO UPDATE would
                    -- preserve the stale NULL and the operator's form
                    -- would blank the rejection / control_sample / pm_*
                    -- row on the next read (matchesBatch can't find it).
                    batch_id    = EXCLUDED.batch_id,
                    recorded_at = NOW()
                RETURNING *
                """,
                new_short_time_id(),
                _job_card_id, _batch_id, _cat, _qty, _uom, _remarks,
                _recorded_by, _material_name, _bom_line_id,
            )
        row = await insert_with_pk_retry(conn, _insert)
        saved.append(_serialize(row))
    return {"saved": True, "rows": saved}


# ---------------------------------------------------------------------------
# Save accounting summary + balance check
# ---------------------------------------------------------------------------

async def save_accounting(conn, *, job_card_id: int,
                          payload: dict,
                          saved_by: str | None = None,
                          batch_id: int | None = None) -> dict:
    """Save the summary row and compute is_balanced + percentages.

    Migration 049 made the row per-(JC, batch). When ``batch_id`` is
    passed, the upsert keys on (job_card_id, COALESCE(batch_id, 0)) so
    each batch keeps its own summary. ``batch_id=None`` upserts the
    legacy JC-level row (still keyed at the COALESCE sentinel 0), which
    older clients hit when they don't know about batches.

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
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    jc = await conn.fetchrow(
        """
        SELECT job_card_id, output_kind, uom, carried_qty_kg, dispatched_to_next_kg,
               bom_id
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

    # ── Balance equation (R9 conservation identity) ──
    # See the module docstring at the top of this file for the convention.
    # The denominator is `total_input` because it is already the
    # comprehensive input for stage N (RM for stage 1, SFG carry-in for
    # stage 2+). carried_in_qty is kept as an audit mirror only - the
    # legacy code persisted it without using it in the balance for the
    # same reason.
    #
    # OUT side = output (mass retained on floor) + dispatched_out (moved
    #            to next JC) + process_loss + extra_give + control_sample
    #            + rejection + offgrade + balance_material + wastage
    #            + total_return (RM sent back to stores / prev stage,
    #                            summed off consumption rows).
    # balance_difference = total_input - OUT.
    total_return = _f(await conn.fetchval(
        "SELECT COALESCE(SUM(return_qty), 0) FROM job_card_material_consumption_v2 WHERE job_card_id=$1",
        job_card_id,
    ))

    total_accounted = (
        output_qty + dispatched_out
        + process_loss + extra_give + control_sample
        + rejection + offgrade + balance_mat + wastage
        + total_return
    )
    diff = round(total_input - total_accounted, 3)

    # ── is_balanced (R9): percentage-based against the per-BOM tolerance,
    # with absolute-qty fallback for zero-input edge cases (e.g. mass-only
    # consolidation stages where total_input is 0). Tolerance reads from
    # bom_header.allowed_balance_tolerance_pct (migration 028) and falls
    # back to BALANCE_TOLERANCE_PCT_DEF when:
    #   * bom_id is NULL on the JC (no BOM linked)
    #   * the column is NULL (shouldn't happen given the 028 NOT NULL DEFAULT
    #     but kept defensive against direct DB pokes).
    tolerance_pct = await conn.fetchval(
        "SELECT allowed_balance_tolerance_pct FROM bom_header WHERE bom_id=$1",
        jc["bom_id"],
    )
    if tolerance_pct is None:
        tolerance_pct = BALANCE_TOLERANCE_PCT_DEF
    tolerance_pct = float(tolerance_pct)

    if total_input > 0:
        is_balanced = (abs(diff) / total_input) <= tolerance_pct
    else:
        is_balanced = abs(diff) <= BALANCE_TOLERANCE_QTY

    # ── Loss percentages — denominator: FG ACTUAL output (output_qty).
    #
    # Operator-stated rules in play:
    #   1. Denominator anchors on output_qty (not total_input), so the
    #      summary stays meaningful on JCs where RM indent is still
    #      empty (e.g. while testing).
    #   2. Off-grade and rejection are the same operational bucket —
    #      total_loss counts offgrade ONCE (rejection is preserved as a
    #      column for back-compat but no longer surfaced separately).
    #   3. Wastage is folded INTO process_loss as a display aggregate —
    #      operators don't distinguish "wastage" from "process loss" in
    #      practice. Storage stays split (process_loss_qty + wastage_qty
    #      remain separate columns so the conservation identity above
    #      still works); only the %-strip combines them.
    #   4. "Other Loss" is retired entirely. other_loss_pct is no longer
    #      computed; it stays in the schema as NULL for back-compat.
    effective_process_loss = process_loss + wastage
    if output_qty > 0:
        process_loss_pct   = round((effective_process_loss / output_qty) * 100, 3)
        ega_loss_pct       = round((extra_give              / output_qty) * 100, 3)
        invisible_loss_pct = round(process_loss_pct + ega_loss_pct, 3)
        total_loss_pct     = round(
            ((effective_process_loss + extra_give + offgrade) / output_qty) * 100, 3,
        )
    else:
        process_loss_pct = total_loss_pct = None
        invisible_loss_pct = None
    other_loss_pct = None   # retired — see rule 4 above.

    # pm_variance_breakdown is intentionally absent from the column list
    # and ON CONFLICT SET: that column is owned by B6 (R11 PM variance
    # store, on the byproducts save path), not by this summary save. The
    # schema default '{}'::jsonb seeds the column on first INSERT, and
    # the ON CONFLICT path doesn't touch it - so a PM variance written
    # by save_byproducts is preserved across a subsequent summary save.
    #
    # ON CONFLICT key matches migration 049's uq_jca_v2_jc_batch
    # (job_card_id, COALESCE(batch_id, 0)). Postgres's ON CONFLICT
    # accepts the unique-index expression list directly — same trick
    # used on consumption / byproducts / balance_materials.
    #
    # Pre-049 environments don't have the batch_id column. Detect that
    # at runtime (cheap: one fetchval against information_schema) and
    # dispatch to the legacy single-row INSERT instead. We CANNOT rely
    # on try/except UndefinedColumnError here because save_accounting
    # often runs inside a caller transaction (close_job_card,
    # save_accounting_summary_v2 route): once asyncpg raises inside
    # that transaction, PG flips the txn to aborted state and any
    # subsequent statement in the same txn errors out. Pre-check
    # avoids the failed-statement-in-txn trap.
    has_batch_col = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name  = 'job_card_accounting_v2'
              AND column_name = 'batch_id'
        )
        """
    )

    if has_batch_col:
        async def _insert():
            return await conn.fetchrow(
                """
                INSERT INTO job_card_accounting_v2 (
                    accounting_id,
                    job_card_id, batch_id,
                    total_input_qty, input_uom,
                    output_qty, output_uom, output_qty_units, output_kind,
                    carried_in_qty, dispatched_out_qty,
                    process_loss_qty, process_loss_breakdown,
                    extra_give_away_qty, balance_material_qty,
                    offgrade_total_qty, rejection_qty, wastage_qty, control_sample_qty,
                    total_accounted_qty, balance_difference_qty, is_balanced,
                    process_loss_pct, other_loss_pct, total_loss_pct,
                    invisible_loss_pct,
                    saved_by
                ) VALUES (
                    $1,
                    $2, $3,
                    $4, $5,
                    $6, $7, $8, $9,
                    $10, $11,
                    $12, $13::jsonb,
                    $14, $15,
                    $16, $17, $18, $19,
                    $20, $21, $22,
                    $23, $24, $25,
                    $26,
                    $27
                )
                ON CONFLICT (job_card_id, COALESCE(batch_id, 0)) DO UPDATE SET
                    batch_id               = EXCLUDED.batch_id,
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
                    invisible_loss_pct     = EXCLUDED.invisible_loss_pct,
                    saved_by               = EXCLUDED.saved_by,
                    saved_at               = NOW()
                RETURNING *
                """,
                new_short_time_id(),
                job_card_id, batch_id,
                total_input, input_uom,
                output_qty, output_uom, output_units, output_kind,
                carried_in, dispatched_out,
                process_loss, json.dumps(process_loss_breakdown),
                extra_give, balance_mat,
                offgrade, rejection, wastage, control_sample,
                total_accounted, diff, is_balanced,
                process_loss_pct, other_loss_pct, total_loss_pct,
                invisible_loss_pct,
                saved_by,
            )
    else:
        # Pre-049 fallback: legacy single-row-per-JC INSERT. Apply
        # migration 049 to enable per-batch accounting; this branch only
        # exists so a not-yet-migrated environment doesn't 500 on
        # Save Output / Complete.
        logger.warning(
            "save_accounting: job_card_accounting_v2.batch_id missing — "
            "writing legacy single-row-per-JC schema. Apply migration 049."
        )
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
                    invisible_loss_pct,
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
                    $25,
                    $26
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
                    invisible_loss_pct     = EXCLUDED.invisible_loss_pct,
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
                invisible_loss_pct,
                saved_by,
            )
    row = await insert_with_pk_retry(conn, _insert)
    # B12: silent variance capture after summary save. Variance values
    # may have shifted if the operator adjusted consumption rows since
    # the previous capture.
    try:
        variance_summary = await _record_consumption_variance(conn, job_card_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("variance capture from summary save failed: %s", exc)
        variance_summary = {"rows": 0, "skipped": [f"wrapper:{exc!r}"]}
    return {
        "saved": True,
        "accounting": _serialize(row),
        "total_accounted_qty": total_accounted,
        "balance_difference_qty": diff,
        "is_balanced": is_balanced,
        "tolerance_pct": tolerance_pct,
        "invisible_loss_pct": invisible_loss_pct,
        "variance": variance_summary,
    }
