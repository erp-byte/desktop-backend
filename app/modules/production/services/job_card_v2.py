"""Plan v2 → Job Card v2 generation + multi-shift time capture.

Targets the v2 tables (migration 017):
    job_card_v2, job_card_shift_log_v2, job_card_partial_dispatch_v2,
    job_card_output_v2, job_card_rm_indent_v2, job_card_pm_indent_v2,
    job_card_sign_off_v2.

v1 job_card and friends are untouched.

Flow:
    production_plan_v2 (approved)
        └── for each production_plan_line_v2
                └── for each production_plan_step_v2 (in step_order)
                        └── job_card_v2  (plan_id, plan_line_id, plan_step_id)

job_card_id is the same 8-digit time-based BIGINT pattern used by
production_plan_v2.plan_id and so_fulfillment_v2.so_fulfillment_id — app
supplies the candidate, PK-retry handles collisions.

Stage chain:
    Stage 1 of each line has input_kind='RM' and starts unlocked.
    Stages 2..N have input_kind='SFG', locked_reason='awaiting_previous_stage'.
    Stage N has output_kind='FG'. Stages 1..N-1 have output_kind='WIP'.
    prev_job_card_id / next_job_card_id wire the chain bi-directionally so
    the partial-dispatch endpoints can hop both ways.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from asyncpg.exceptions import UndefinedColumnError

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.core.warehouse_scope import WAREHOUSE_NORM_ANY_SQL, WAREHOUSE_NORM_SQL
from app.modules.production.services.output_calc import compute_output_row

logger = logging.getLogger(__name__)

# Stage 2 fallback: migration 038 adds batch_id to the four accounting
# tables.  When the server is restarted on Stage 2 code BEFORE the
# migration lands, the detail SELECTs catch UndefinedColumnError and
# fall back to a NULL batch_id projection.  This set tracks which
# tables we've already logged about so a steady-state polling client
# doesn't flood the log on every JC detail load.
_BATCH_ID_MISSING_LOGGED: set[str] = set()


def _warn_batch_id_missing_once(table: str) -> None:
    if table in _BATCH_ID_MISSING_LOGGED:
        return
    _BATCH_ID_MISSING_LOGGED.add(table)
    logger.warning(
        "%s.batch_id does not exist — apply migration 038_jc_batch_per_record.sql "
        "to enable Stage 2 per-batch accounting. JC detail reads will return "
        "batch_id=NULL until then; new writes still default to the JC's open "
        "batch but the column simply isn't there to persist the tag yet.",
        table,
    )


# ---------------------------------------------------------------------------
# R6 lock guard
#
# Operational entry (output, accounting, annexures, shifts/start) is refused
# while a JC is locked unless force_unlocked is set. The router-level
# dispatcher translates the returned error dict into a 409 / 404 HTTP code -
# this helper stays HTTP-agnostic so jc_accounting_v2.py and jc_annexures_v2.py
# can call it without pulling in fastapi.
#
# Exempt operations (PATCH/DELETE header edits, force-unlock, sign-off,
# complete, close, dispatch-to-next, receive-material) deliberately do NOT
# call this gate - they govern the lock itself or are lifecycle transitions.
# ---------------------------------------------------------------------------

async def assert_not_locked(conn, job_card_id: int) -> dict | None:
    """R6 lock guard. Returns None when the JC is unlocked or force-unlocked
    (caller proceeds). Returns an error dict when the JC is locked, missing,
    or soft-deleted (caller bails by returning the dict).
    """
    row = await conn.fetchrow(
        "SELECT is_locked, locked_reason, force_unlocked, status "
        "FROM   job_card_v2 "
        "WHERE  job_card_id=$1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not row:
        return {"error": "job_card_not_found"}
    if row["is_locked"] and not row["force_unlocked"]:
        return {
            "error": "locked",
            "locked_reason": row["locked_reason"],
            "status": row["status"],
            "message": "Job card is locked; operational entry refused. "
                       "Use force-unlock with approval if required.",
        }
    return None


# ---------------------------------------------------------------------------
# R11 packing-stage detection
#
# EGA (Extra Give Away) is only meaningful at packing stages - that's where
# the operator over-packs slightly to compensate for label-weight tolerance.
#
# B6 C2 fix: instead of exact-string matching against truncated-paren
# legacy values, normalise the stage and match any string containing
# 'packaging' or 'packing'. This is resilient to data cleanups (e.g.
# closing the paren on 'flavouring_(bulk_packaging)') and case drift.
# ---------------------------------------------------------------------------
_PACKING_STAGE_TOKENS = ("packaging", "packing")

def is_packing_stage(stage: str | None) -> bool:
    if not stage:
        return False
    s = stage.strip().lower()
    return any(tok in s for tok in _PACKING_STAGE_TOKENS)

# Legacy alias - some external code may import this. Kept for back-compat.
PACKING_STAGES = frozenset({
    "packaging",
    "flavouring_(bulk_packaging",
    "roasting_(bulk_packaging",
})


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


def _batch_number(plan_id: int, plan_line_id: int, step_order: int) -> str:
    """Stable, unique-enough batch label. Format: P{plan_id}-L{plan_line_id}-S{step_order}."""
    return f"P{plan_id}-L{plan_line_id}-S{step_order}"


# Local helper so get_job_card() can pull annexure rows without importing
# the whole jc_annexures_v2 module up-front (avoids a circular import if
# annexure code ever imports from here). Returns active rows only — the
# soft-delete filter mirrors the public list_* helpers in jc_annexures_v2.
_ANNEXURE_TABLES = {
    'metal_detection':     'job_card_metal_detection_v2',
    'weight_check':        'job_card_weight_check_v2',
    'environment':         'job_card_environment_v2',
    'loss_reconciliation': 'job_card_loss_reconciliation_v2',
    'remarks':             'job_card_remarks_v2',
}


async def _annexure_rows(conn, kind: str, job_card_id: int) -> list[dict]:
    table = _ANNEXURE_TABLES.get(kind)
    if not table:
        return []
    rows = await conn.fetch(
        f"SELECT * FROM {table} WHERE job_card_id = $1 AND deleted_at IS NULL "
        f"ORDER BY recorded_at",
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# Lightweight wrapper around jc_additives_v2.list_additives so
# get_job_card can stay self-contained (no need to deal with module-
# import ordering between job_card_v2 and jc_additives_v2). The actual
# query lives in the dedicated service; this helper just hands the
# connection over.
async def _list_additives_local(conn, job_card_id: int) -> list[dict]:
    from app.modules.production.services.jc_additives_v2 import list_additives
    return await list_additives(conn, job_card_id)


# ---------------------------------------------------------------------------
# Plan → JCs
# ---------------------------------------------------------------------------

# ── UOM normalisation for v2 indent tables ────────────────────────────────
#
# bom_line.uom is free-form text ("kg", "Kg", "pcs", …) while
# job_card_rm_indent_v2 / pm_indent_v2 enforce CHECK constraints over a
# fixed vocabulary. Map common BOM aliases to the canonical v2 token; if
# the result isn't acceptable for the target kind, fall back to KGS so
# the INSERT doesn't blow up the entire JC-creation transaction.

_UOM_ALIAS: dict[str, str] = {
    'KG': 'KGS', 'KGS': 'KGS', 'KILOGRAM': 'KGS', 'KILOGRAMS': 'KGS',
    'G': 'GMS', 'GM': 'GMS', 'GMS': 'GMS', 'GRAM': 'GMS', 'GRAMS': 'GMS',
    'L': 'LTRS', 'LT': 'LTRS', 'LTR': 'LTRS', 'LTRS': 'LTRS',
    'LITRE': 'LTRS', 'LITRES': 'LTRS', 'LITER': 'LTRS', 'LITERS': 'LTRS',
    'NO': 'NOS', 'NOS': 'NOS', 'PC': 'PCS', 'PCS': 'PCS',
    'PIECE': 'PCS', 'PIECES': 'PCS',
    'ROLL': 'ROLL', 'ROLLS': 'ROLL',
    'SET': 'SETS', 'SETS': 'SETS',
    'BUNDLE': 'BUNDLE', 'BUNDLES': 'BUNDLE',
}

_RM_ALLOWED = {'KGS', 'GMS', 'LTRS', 'NOS'}
_PM_ALLOWED = {'KGS', 'NOS', 'ROLL', 'SETS', 'PCS', 'BUNDLE'}


def _canonical_uom(raw: str | None, kind: str) -> str:
    """Map a free-form BOM uom to the v2 CHECK-constraint vocabulary for
    the given kind ('rm' / 'pm'). Unknown values fall back to KGS so the
    INSERT doesn't violate the CHECK constraint and abort the
    transaction — operators can edit the indent row to fix the UOM
    later. Logs a warning when a fallback fires."""
    if not raw:
        return 'KGS'
    canon = _UOM_ALIAS.get(raw.strip().upper(), raw.strip().upper())
    allowed = _RM_ALLOWED if kind == 'rm' else _PM_ALLOWED
    if canon in allowed:
        return canon
    logger.warning("Unrecognised bom uom %r for kind %s; falling back to KGS", raw, kind)
    return 'KGS'


async def resolve_bom_multiplier(
    conn,
    *,
    fg_sku_name: str | None,
    qty_kg,
    qty_units=None,
) -> tuple[float, str, float | None]:
    """Return ``(multiplier, basis, sku_uom)`` for BOM math.

    Convention (operator-stated, supersedes earlier 'per kg of FG' docstring):

      • ``all_sku.uom`` is the per-unit kg multiplier of the FG SKU
        (schema.sql:54, R2 framework single source of truth).
      • When ``uom != 1.000`` (per-piece FG SKU, e.g. 500 gm pouch with
        ``uom = 0.5``), BOM ``quantity_per_unit`` is interpreted as
        'per FG unit'. ``multiplier = qty_units`` (= qty_kg / uom when
        the caller passed only kg).
      • When ``uom == 1.000`` (1 piece = 1 kg), units and kg are
        numerically equal, so either multiplier yields the same answer;
        we pick kg for back-compat with the prior convention.
      • When the FG SKU is missing from ``all_sku`` or its uom is NULL,
        fall back to kg.

    Callers pick which qty to pass — indent generators pass planned qty
    (no actual exists yet at JC creation); the variance calc passes the
    actual FG output (falling back to planned when no output is
    recorded).

    Returns ``(multiplier, basis, sku_uom)`` where ``basis`` is one of
    ``'units' | 'kg'`` (mostly for tracing / log lines) and ``sku_uom``
    is the resolved per-unit kg (``None`` when the SKU isn't in the
    master).
    """
    try:
        kg = float(qty_kg or 0)
    except (TypeError, ValueError):
        kg = 0.0
    if kg <= 0:
        return (0.0, 'kg', None)

    sku_uom: float | None = None
    if fg_sku_name:
        row = await conn.fetchrow(
            "SELECT uom FROM all_sku WHERE particulars = $1 LIMIT 1",
            fg_sku_name,
        )
        if row and row["uom"] is not None:
            try:
                sku_uom = float(row["uom"])
            except (TypeError, ValueError):
                sku_uom = None

    # uom missing / zero / exactly 1 → kg-basis (canonical legacy)
    if sku_uom is None or sku_uom <= 0 or abs(sku_uom - 1.0) < 1e-9:
        return (kg, 'kg', sku_uom)

    # uom != 1 → units-basis. Prefer the caller-supplied units; else derive.
    if qty_units is not None:
        try:
            units = float(qty_units)
            if units > 0:
                return (units, 'units', sku_uom)
        except (TypeError, ValueError):
            pass
    return (kg / sku_uom, 'units', sku_uom)


async def _materialise_indents(
    conn,
    *,
    job_card_id: int,
    bom_id: int,
    planned_qty_kg,
    is_first_stage: bool,
    fg_sku_name: str | None = None,
    planned_qty_units=None,
    include_rm: bool = True,
    include_pm: bool = True,
) -> tuple[int, int]:
    """Create per-JC v2 indent rows from the bom_line catalogue.

    Spec (per ops 2026-05): the entire RM **and** PM issuance happens on
    the first stage of the plan-line chain. Stage 1 receives both, then
    its output flows through dispatch_to_next as WIP/SFG through every
    subsequent stage until the last produces the FG. Middle and final
    stages receive **no** fresh issuance — they consume upstream WIP, not
    new RM/PM.

    This was previously split (RM on first, PM on last) which left the
    PM dropdown empty on a stage-1 JC and impossible for the operator to
    populate any rejection / balance / extra-giveaway against a PM
    article. Migration 022 relocates legacy PM rows from last-stage JCs
    back to their corresponding stage-1 JC.

    Returns (rm_count, pm_count). `include_rm` / `include_pm` let the
    backfill caller insert only the side that's actually missing (the
    other side may already have operator-touched rows that must be
    preserved).

    bom_line.quantity_per_unit basis depends on the FG SKU's all_sku.uom
    (resolved by resolve_bom_multiplier above):

      • all_sku.uom != 1  → per-piece FG SKU; qpu is 'per FG unit';
        multiplier = planned_qty_units (= planned_qty_kg / uom when
        the planner only entered kg).
      • all_sku.uom == 1 or NULL → qpu is 'per kg of FG';
        multiplier = planned_qty_kg.

    gross_qty applies the loss buffer: reqd / (1 - loss_pct/100).
    """
    if not is_first_stage:
        return (0, 0)
    if not bom_id or planned_qty_kg is None or float(planned_qty_kg) <= 0:
        return (0, 0)

    bom_lines = await conn.fetch(
        """
        SELECT bom_line_id, material_sku_name, item_type, uom,
               quantity_per_unit, loss_pct, godown
        FROM   bom_line
        WHERE  bom_id = $1
        ORDER  BY line_number
        """,
        bom_id,
    )
    if not bom_lines:
        return (0, 0)

    # Indent runs at JC creation — no actual FG output yet, so we pass
    # planned qty as the multiplier source. The variance calc later swaps
    # in the actual FG output.
    multiplier, basis, sku_uom = await resolve_bom_multiplier(
        conn,
        fg_sku_name=fg_sku_name,
        qty_kg=planned_qty_kg,
        qty_units=planned_qty_units,
    )
    if multiplier <= 0:
        return (0, 0)
    logger.debug(
        "indent: jc=%s fg=%s sku_uom=%s multiplier=%.3f basis=%s",
        job_card_id, fg_sku_name, sku_uom, multiplier, basis,
    )
    rm_count = 0
    pm_count = 0

    for bl in bom_lines:
        item_type = (bl["item_type"] or "").strip().lower()
        if item_type == 'rm' and not include_rm:
            continue
        if item_type == 'pm' and not include_pm:
            continue
        if item_type not in ('rm', 'pm'):
            continue   # ignore unknown line types — bom may carry meta lines

        qpu = float(bl["quantity_per_unit"] or 0)
        if qpu <= 0:
            continue
        reqd  = qpu * multiplier
        loss  = float(bl["loss_pct"] or 0)
        gross = reqd / (1 - loss / 100.0) if loss < 100 else reqd
        if reqd <= 0:
            continue
        uom = _canonical_uom(bl["uom"], item_type)

        if item_type == 'rm':
            async def _insert_rm(_bl=bl, _reqd=reqd, _gross=gross, _loss=loss, _uom=uom):
                return await conn.fetchval(
                    """
                    INSERT INTO job_card_rm_indent_v2 (
                        rm_indent_id, job_card_id, bom_line_id,
                        material_sku_name, uom,
                        reqd_qty, loss_pct, gross_qty, godown, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending')
                    RETURNING rm_indent_id
                    """,
                    new_short_time_id(), job_card_id, _bl["bom_line_id"],
                    _bl["material_sku_name"], _uom,
                    round(_reqd, 3), _loss, round(_gross, 3), _bl["godown"],
                )
            await insert_with_pk_retry(conn, _insert_rm)
            rm_count += 1
        else:   # 'pm'
            async def _insert_pm(_bl=bl, _reqd=reqd, _gross=gross, _loss=loss, _uom=uom):
                return await conn.fetchval(
                    """
                    INSERT INTO job_card_pm_indent_v2 (
                        pm_indent_id, job_card_id, bom_line_id,
                        material_sku_name, uom,
                        reqd_qty, loss_pct, gross_qty, godown, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending')
                    RETURNING pm_indent_id
                    """,
                    new_short_time_id(), job_card_id, _bl["bom_line_id"],
                    _bl["material_sku_name"], _uom,
                    round(_reqd, 3), _loss, round(_gross, 3), _bl["godown"],
                )
            await insert_with_pk_retry(conn, _insert_pm)
            pm_count += 1

    return (rm_count, pm_count)


async def upsert_consumption_lines(
    conn,
    *,
    job_card_id: int,
    entries: list[dict],
    input_kind: str,
    recorded_by: str | None,
    batch_id: int | None = None,
) -> int:
    """Write per-BOM-line consumption rows for a JC.

    `entries` is the list of {bom_line_id, material_sku_name, consumed_qty,
    uom, remarks} dicts the UI sends in rm_consumed / pm_consumed.
    `input_kind` is the consumption kind: 'RM' or 'PM' (RM/PM come from
    the BOM catalog; SFG/WIP rows are written separately by the stage-
    handoff code path, not this function).

    Stage 2: rows are now tagged with batch_id.  The UNIQUE key on the
    table changed from (job_card_id, material_sku_name) to
    (job_card_id, COALESCE(batch_id, 0), material_sku_name) — same
    material can appear once per batch.  When `batch_id` is None
    (legacy code path), rows fall into the `0` bucket and the legacy
    upsert behaviour is preserved.

    Per-row semantics:
      * issued_qty defaults to 0 here because Material Consumption is
        an output-side ledger; the issued column is filled from the
        indent row at the stage that materialised the indent. Detail
        readers join the two when they need the variance.
      * uom is taken from the entry; we trust the UI's UomRules to have
        picked a value that's in the v2 CHECK list.

    Returns the count of rows inserted/updated.
    """
    if input_kind not in ('RM', 'PM'):
        return 0
    written = 0
    for e in entries:
        sku = e.get("material_sku_name")
        qty = e.get("consumed_qty")
        if not sku or qty is None:
            continue   # skip malformed entries silently — backend validated
                       # bom_line_id at the router layer already
        bom_line_id = e.get("bom_line_id")
        uom         = e.get("uom") or "KGS"
        remarks     = e.get("remarks")
        async def _upsert(_sku=sku, _kind=input_kind, _uom=uom,
                          _qty=qty, _bom_id=bom_line_id, _rem=remarks,
                          _rec_by=recorded_by, _batch=batch_id):
            # ON CONFLICT references the expression UNIQUE INDEX
            # uq_jcmc_v2_jc_batch_material (migration 038) by its
            # column list — PG matches the index automatically when
            # the conflict_target columns + expressions match.
            return await conn.fetchval(
                """
                INSERT INTO job_card_material_consumption_v2 (
                    consumption_id, job_card_id, bom_line_id, batch_id,
                    material_sku_name, input_kind, uom,
                    issued_qty, actual_consumed_qty, return_qty,
                    remarks, recorded_by
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, 0, $9, $10)
                ON CONFLICT (job_card_id, COALESCE(batch_id, 0),
                             material_sku_name)
                DO UPDATE SET
                    bom_line_id         = EXCLUDED.bom_line_id,
                    input_kind          = EXCLUDED.input_kind,
                    uom                 = EXCLUDED.uom,
                    actual_consumed_qty = EXCLUDED.actual_consumed_qty,
                    remarks             = EXCLUDED.remarks,
                    recorded_by         = EXCLUDED.recorded_by
                RETURNING consumption_id
                """,
                new_short_time_id(), job_card_id, _bom_id, _batch,
                _sku, _kind, _uom, float(_qty), _rem, _rec_by,
            )
        await insert_with_pk_retry(conn, _upsert)
        written += 1
    return written


async def backfill_indents_for_jc(conn, job_card_id: int) -> dict:
    """Retro-materialise indent rows for a v2 JC that was created before
    indent materialisation was wired in. Idempotent: if the JC already
    has any RM or PM indent rows, this is a no-op for that side.

    Returns a verbose diagnostic dict so a no-op tells the caller WHY —
    the four common failure modes (missing bom_id, missing bom_lines,
    zero planned_qty_kg, wrong stage) all produce (rm_added=0, pm_added=0)
    but only one is the actual cause, and the operator deserves a hint.
    Falls back to JC.plan_line.bom_id when the JC itself has bom_id NULL
    (older JCs created before the column was wired up).
    """
    jc = await conn.fetchrow(
        """
        SELECT j.job_card_id, j.bom_id, j.plan_line_id, j.planned_qty_kg,
               j.planned_qty_units, j.fg_sku_name,
               j.step_number,
               (SELECT MAX(step_number) FROM job_card_v2
                 WHERE plan_line_id = j.plan_line_id
                   AND deleted_at IS NULL) AS max_step,
               pl.bom_id AS plan_line_bom_id,
               pl.planned_qty_kg AS plan_line_planned_qty_kg
        FROM   job_card_v2 j
        LEFT   JOIN production_plan_line_v2 pl
                ON pl.plan_line_id = j.plan_line_id
        WHERE  j.job_card_id = $1 AND j.deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}

    has_rm = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_rm_indent_v2 WHERE job_card_id=$1",
        job_card_id,
    )
    has_pm = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_pm_indent_v2 WHERE job_card_id=$1",
        job_card_id,
    )

    is_first = jc["step_number"] == 1
    # is_last is retained in the response for the operator's situational
    # awareness, but no longer drives PM materialisation — PM now goes
    # on stage 1 alongside RM.
    is_last  = jc["step_number"] == jc["max_step"]

    # ── Resolve effective bom_id ───────────────────────────────────────
    # Older JCs were created before job_card_v2.bom_id was being populated
    # from production_plan_line_v2.bom_id. Fall back to the plan-line value
    # and (if it differs) repair the JC row in place so future reads no
    # longer need the join.
    effective_bom_id = jc["bom_id"] or jc["plan_line_bom_id"]
    if jc["bom_id"] is None and jc["plan_line_bom_id"] is not None:
        await conn.execute(
            "UPDATE job_card_v2 SET bom_id = $1 WHERE job_card_id = $2",
            jc["plan_line_bom_id"], job_card_id,
        )

    effective_qty = jc["planned_qty_kg"]
    if (effective_qty is None or float(effective_qty) <= 0) and jc["plan_line_planned_qty_kg"]:
        effective_qty = jc["plan_line_planned_qty_kg"]

    # ── Count bom_lines available so the response can pinpoint a missing-BOM cause ──
    bom_line_count = 0
    rm_bom_count = 0
    pm_bom_count = 0
    if effective_bom_id:
        bom_line_count = await conn.fetchval(
            "SELECT COUNT(*) FROM bom_line WHERE bom_id = $1", effective_bom_id,
        ) or 0
        rm_bom_count = await conn.fetchval(
            "SELECT COUNT(*) FROM bom_line WHERE bom_id = $1 AND LOWER(item_type) = 'rm'",
            effective_bom_id,
        ) or 0
        pm_bom_count = await conn.fetchval(
            "SELECT COUNT(*) FROM bom_line WHERE bom_id = $1 AND LOWER(item_type) = 'pm'",
            effective_bom_id,
        ) or 0

    # ── Actually try to insert ───────────────────────────────────────
    # Stage-1-only: RM and PM both belong on the first stage. On a
    # non-stage-1 JC there's nothing to do — operators see all BOM
    # articles via the catalog field on the detail response anyway.
    rm_count = 0
    pm_count = 0
    if is_first:
        rm_count, pm_count = await _materialise_indents(
            conn, job_card_id=job_card_id, bom_id=effective_bom_id,
            planned_qty_kg=effective_qty,
            is_first_stage=True,
            fg_sku_name=jc["fg_sku_name"],
            planned_qty_units=jc["planned_qty_units"],
            include_rm=not has_rm,
            include_pm=not has_pm,
        )

    # ── Explain a no-op ──────────────────────────────────────────────
    diagnosis: list[str] = []
    if not is_first:
        diagnosis.append(
            f"step_number={jc['step_number']} is not the first stage — "
            "RM+PM both materialise on stage 1 only"
        )
    if effective_bom_id is None:
        diagnosis.append("no bom_id on JC or plan_line — can't look up BOM")
    if effective_qty is None or float(effective_qty) <= 0:
        diagnosis.append("planned_qty_kg is null/zero — can't compute reqd quantities")
    if effective_bom_id and bom_line_count == 0:
        diagnosis.append(f"bom_line is empty for bom_id={effective_bom_id}")
    if is_first and rm_bom_count == 0:
        diagnosis.append("BOM has no item_type='rm' rows")
    if is_first and pm_bom_count == 0:
        diagnosis.append("BOM has no item_type='pm' rows")

    return {
        "job_card_id":      job_card_id,
        "rm_added":         rm_count,
        "pm_added":         pm_count,
        "rm_skipped":       bool(has_rm),
        "pm_skipped":       bool(has_pm),
        "diagnosis":        diagnosis if (rm_count == 0 and pm_count == 0
                                          and not has_rm and not has_pm) else [],
        "effective_bom_id": effective_bom_id,
        "effective_qty_kg": float(effective_qty) if effective_qty is not None else None,
        "step_number":      jc["step_number"],
        "max_step":         jc["max_step"],
        "is_first":         is_first,
        "is_last":          is_last,
        "bom_line_count":   bom_line_count,
        "rm_bom_count":     rm_bom_count,
        "pm_bom_count":     pm_bom_count,
    }


async def get_plan_job_card_groups(conn, plan_id: int) -> dict:
    """Job cards for a plan, grouped per plan-line (one product per group)
    with a per-group summary.

    A daily plan can bundle many products (e.g. plan 58795071 = 15 SKUs / 33
    stages). Returned flat, their stage chains interleave and the operator
    can't follow any one product's Sorting→Packaging sequence. This groups the
    cards so the UI can render each product as its own collapsible section.

    Groups are ordered by planned_qty_kg DESC (largest run first); stages
    within a group are ordered by step_number (the corrected chain order).
    Returns ``{"plan_id", "group_count", "groups": [...]}``; ``groups`` is the
    empty list for an unknown / JC-less plan (callers show "no job cards").
    """
    rows = await conn.fetch(
        """
        SELECT plan_line_id, job_card_id, job_card_number,
               step_number, process_name, stage, status, is_locked,
               floor, planned_qty_kg, planned_qty_units,
               fg_sku_name, customer_name, batch_number
        FROM   job_card_v2
        WHERE  plan_id = $1 AND deleted_at IS NULL
        ORDER  BY planned_qty_kg DESC NULLS LAST, plan_line_id, step_number
        """,
        plan_id,
    )
    groups: dict = {}
    order: list = []
    for r in rows:
        pl = r["plan_line_id"]
        g = groups.get(pl)
        if g is None:
            g = {
                "plan_line_id":      pl,
                "fg_sku_name":       r["fg_sku_name"],
                "customer_name":     r["customer_name"],
                "planned_qty_kg":    float(r["planned_qty_kg"]) if r["planned_qty_kg"] is not None else None,
                "planned_qty_units": r["planned_qty_units"],
                "stage_count":       0,
                "completed_count":   0,
                "stages":            [],
            }
            groups[pl] = g
            order.append(pl)
        g["stages"].append({
            "job_card_id":     r["job_card_id"],
            "job_card_number": r["job_card_number"],
            "step_number":     r["step_number"],
            "process_name":    r["process_name"],
            "stage":           r["stage"],
            "status":          r["status"],
            "is_locked":       r["is_locked"],
            "floor":           r["floor"],
            "batch_number":    r["batch_number"],
        })
        g["stage_count"] += 1
        if r["status"] in ("completed", "closed"):
            g["completed_count"] += 1
    return {
        "plan_id":     plan_id,
        "group_count": len(order),
        "groups":      [groups[pl] for pl in order],
    }


async def create_job_cards_from_plan(conn, plan_id: int) -> dict:
    """Generate one job_card_v2 per (line × step) for the given plan.

    Idempotent guard: if any JC already exists for plan_id, this refuses.
    Re-generating requires cancelling the existing ones first.

    Returns:
        {"plan_id": ..., "lines": [{"plan_line_id": ..., "job_card_ids": [...]}, ...]}
    """
    plan = await conn.fetchrow(
        "SELECT plan_id, entity, warehouse FROM production_plan_v2 WHERE plan_id=$1",
        plan_id,
    )
    if not plan:
        return {"error": "plan_not_found"}

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_v2 WHERE plan_id=$1 AND deleted_at IS NULL",
        plan_id,
    )
    if existing and existing > 0:
        return {"error": "job_cards_already_exist", "count": existing}

    line_rows = await conn.fetch(
        """
        SELECT plan_line_id, fg_sku_name, customer_name, bom_id,
               planned_qty_kg, planned_qty_units
        FROM production_plan_line_v2
        WHERE plan_id=$1
        ORDER BY plan_line_id
        """,
        plan_id,
    )

    factory = plan["warehouse"]
    entity  = plan["entity"]
    result_lines: list[dict] = []

    for ln in line_rows:
        plan_line_id = ln["plan_line_id"]

        step_rows = await conn.fetch(
            """
            SELECT step_id, step_order, process_name, stage, floor
            FROM production_plan_step_v2
            WHERE plan_line_id=$1
            ORDER BY step_order
            """,
            plan_line_id,
        )
        if not step_rows:
            result_lines.append({
                "plan_line_id": plan_line_id,
                "job_card_ids": [],
                "skipped": "no_steps",
            })
            continue

        step_count = len(step_rows)
        jc_ids: list[int] = []
        prev_jc_id: int | None = None

        for idx, step in enumerate(step_rows):
            is_first = idx == 0
            is_last  = idx == step_count - 1

            # Material-flow context per spec (see module docstring).
            input_kind  = 'RM'  if is_first else 'SFG'
            output_kind = 'FG'  if is_last  else 'WIP'

            # Lock state: stage 1 opens immediately so floor can receive RM.
            # Downstream stages stay locked until prev-stage handoff fires.
            is_locked     = not is_first
            status        = 'unlocked' if is_first else 'locked'
            locked_reason = None if is_first else 'awaiting_previous_stage'

            jc_number = f"PLAN-{plan_id}-L{plan_line_id}-S{step['step_order']}"
            batch_no  = _batch_number(plan_id, plan_line_id, step['step_order'])

            # Bind loop vars into the closure (default-arg trick) so each
            # insert call sees its own values, not the last-iteration ones.
            async def _insert(
                _step=step, _jc_number=jc_number, _batch_no=batch_no,
                _is_locked=is_locked, _status=status, _locked_reason=locked_reason,
                _input_kind=input_kind, _output_kind=output_kind, _prev=prev_jc_id,
            ):
                candidate = new_short_time_id()
                return await conn.fetchval(
                    """
                    INSERT INTO job_card_v2 (
                        job_card_id, job_card_number,
                        plan_id, plan_line_id, plan_step_id, bom_id,
                        step_number, process_name, stage,
                        fg_sku_name, customer_name, batch_number,
                        planned_qty_kg, planned_qty_units, uom,
                        input_kind, output_kind,
                        factory, floor, entity,
                        is_locked, locked_reason, status,
                        prev_job_card_id
                    ) VALUES (
                        $1, $2,
                        $3, $4, $5, $6,
                        $7, $8, $9,
                        $10, $11, $12,
                        $13, $14, $15,
                        $16, $17,
                        $18, $19, $20,
                        $21, $22, $23,
                        $24
                    )
                    RETURNING job_card_id
                    """,
                    candidate, _jc_number,
                    plan_id, plan_line_id, _step["step_id"], ln["bom_id"],
                    _step["step_order"], _step["process_name"],
                    # job_card_v2.stage is NOT NULL. plan_steps created
                    # via /lines/{id}/steps before the stage auto-derive
                    # landed (or hand-loaded into the DB) can have NULL
                    # stage; derive from process_name on the fly so the
                    # approve doesn't 500 on those rows. New saves go
                    # through plan_v2.add_step which derives at insert
                    # time, so this is purely curative for legacy data.
                    (_step["stage"]
                     or (_step["process_name"] or "").strip().lower().replace(" ", "_")
                     or "unknown"),
                    ln["fg_sku_name"], ln["customer_name"], _batch_no,
                    ln["planned_qty_kg"], ln["planned_qty_units"], 'KGS',
                    _input_kind, _output_kind,
                    factory, _step["floor"], entity,
                    _is_locked, _locked_reason, _status,
                    _prev,
                )
            jc_id = await insert_with_pk_retry(conn, _insert)
            jc_ids.append(jc_id)

            # Materialise per-JC indent rows from the BOM catalogue.
            # Both RM AND PM rows go on the first stage — see the
            # _materialise_indents docstring. Stages 2+ receive nothing
            # (they consume upstream WIP via dispatch_to_next).
            if ln["bom_id"] and is_first:
                await _materialise_indents(
                    conn,
                    job_card_id=jc_id,
                    bom_id=ln["bom_id"],
                    planned_qty_kg=ln["planned_qty_kg"],
                    is_first_stage=True,
                    fg_sku_name=ln["fg_sku_name"],
                    planned_qty_units=ln["planned_qty_units"],
                )

            # Bi-directional chain — set next pointer on the prev row.
            if prev_jc_id is not None:
                await conn.execute(
                    "UPDATE job_card_v2 SET next_job_card_id=$1 WHERE job_card_id=$2",
                    jc_id, prev_jc_id,
                )

            prev_jc_id = jc_id

        result_lines.append({"plan_line_id": plan_line_id, "job_card_ids": jc_ids})

    logger.info(
        "Generated v2 job cards for plan_id=%d: %d line(s), %d JC(s) total",
        plan_id,
        len(result_lines),
        sum(len(l["job_card_ids"]) for l in result_lines),
    )
    return {"plan_id": plan_id, "lines": result_lines}


# ---------------------------------------------------------------------------
# Plan close: derive from JC close
# ---------------------------------------------------------------------------

async def maybe_close_plan_from_jcs(conn, plan_id: int) -> bool:
    """If every job_card_v2 on this plan is in a terminal state
    ('closed' or 'cancelled'), flip the plan to 'executed'. Returns True
    when a transition happened. Idempotent — already-executed / cancelled
    plans are left alone.
    """
    plan = await conn.fetchrow(
        "SELECT status FROM production_plan_v2 WHERE plan_id=$1",
        plan_id,
    )
    if not plan or plan["status"] in ('executed', 'cancelled'):
        return False

    counts = await conn.fetchrow(
        """
        SELECT
            COUNT(*)                                          AS total,
            COUNT(*) FILTER (WHERE status IN ('closed','cancelled'))
                                                              AS terminal
        FROM job_card_v2
        WHERE plan_id=$1 AND deleted_at IS NULL
        """,
        plan_id,
    )
    if not counts or counts["total"] == 0:
        return False
    if counts["terminal"] != counts["total"]:
        return False

    await conn.execute(
        "UPDATE production_plan_v2 SET status='executed' WHERE plan_id=$1",
        plan_id,
    )
    logger.info("plan_id=%d auto-transitioned to 'executed' (all v2 JCs closed)", plan_id)
    return True


# ---------------------------------------------------------------------------
# Shift-log service (v2)
# ---------------------------------------------------------------------------

VALID_SHIFTS = ('A', 'B', 'C', 'general')


async def start_shift(conn, *, job_card_id: int, shift: str,
                      shift_date, operator_name: str | None = None,
                      notes: str | None = None) -> dict:
    """Open a new shift segment on the given JC. Refuses (open_segment_exists)
    when another segment is currently open — must stop the prior one first.

    The partial-unique index `uq_jcsl_v2_one_open` in migration 017 enforces
    this at the DB level too, so concurrent open attempts cleanly violate.
    """
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    if shift not in VALID_SHIFTS:
        return {"error": "invalid_shift",
                "message": f"shift must be one of {VALID_SHIFTS}"}

    open_row = await conn.fetchrow(
        """
        SELECT log_id FROM job_card_shift_log_v2
        WHERE job_card_id=$1 AND end_at IS NULL
        LIMIT 1
        """,
        job_card_id,
    )
    if open_row:
        return {"error": "open_segment_exists",
                "message": "Stop the currently-open segment before starting a new one",
                "open_log_id": open_row["log_id"]}

    async def _insert_shift():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_shift_log_v2 (
                log_id, job_card_id, shift, shift_date, start_at, operator_name, notes
            ) VALUES ($1, $2, $3, $4, NOW(), $5, $6)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, shift, shift_date, operator_name, notes,
        )
    inserted = await insert_with_pk_retry(conn, _insert_shift)
    # First start_shift on the JC stamps the headline start_time and moves
    # the lifecycle into 'in_progress'. Subsequent shifts don't overwrite.
    await conn.execute(
        """
        UPDATE job_card_v2
           SET start_time = COALESCE(start_time, NOW()),
               status     = CASE
                              WHEN status IN ('locked','unlocked','assigned','material_received')
                                THEN 'in_progress'
                              ELSE status
                            END
         WHERE job_card_id=$1
        """,
        job_card_id,
    )
    return {"opened": True, "log": _serialize(inserted)}


async def stop_shift(conn, *, log_id: int, paused_minutes: int = 0,
                     notes: str | None = None) -> dict:
    """Close an open segment. Recomputes job_card_v2.total_time_min from
    the sum of (end_at - start_at) - paused across every closed segment
    on the JC."""
    if paused_minutes < 0:
        return {"error": "negative_pause"}

    log = await conn.fetchrow(
        "SELECT job_card_id, end_at FROM job_card_shift_log_v2 WHERE log_id=$1",
        log_id,
    )
    if not log:
        return {"error": "log_not_found"}
    if log["end_at"] is not None:
        return {"error": "already_closed"}

    closed = await conn.fetchrow(
        """
        UPDATE job_card_shift_log_v2
           SET end_at         = NOW(),
               paused_minutes = $2,
               notes          = COALESCE($3, notes)
         WHERE log_id=$1
        RETURNING *
        """,
        log_id, paused_minutes, notes,
    )

    total = await conn.fetchval(
        """
        SELECT COALESCE(SUM(
                   EXTRACT(EPOCH FROM (end_at - start_at)) / 60.0
                 - paused_minutes
               ), 0)
        FROM job_card_shift_log_v2
        WHERE job_card_id=$1 AND end_at IS NOT NULL
        """,
        log["job_card_id"],
    )
    await conn.execute(
        "UPDATE job_card_v2 SET total_time_min=$2 WHERE job_card_id=$1",
        log["job_card_id"], total,
    )

    return {"closed": True, "log": _serialize(closed), "total_time_min": float(total)}


async def list_shifts(conn, job_card_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT log_id, job_card_id, shift, shift_date, start_at, end_at,
               paused_minutes, operator_name, notes, created_at
        FROM job_card_shift_log_v2
        WHERE job_card_id=$1
        ORDER BY start_at
        """,
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ---------------------------------------------------------------------------
# JC list (v2)
# ---------------------------------------------------------------------------

_JC_SORTABLE_COLUMNS = frozenset({
    "created_at", "start_time", "end_time", "plan_id", "status",
    "step_number", "job_card_id", "planned_qty_kg",
    # plan_date lives on production_plan_v2 — see _JC_SORT_EXPR below
    # for how the ORDER BY routes around the table alias.
    "plan_date",
})

# Map sortable column → SQL expression. Most are jc.<col>; plan_date
# routes to the subquery so the operator can sort the JC list by the
# planning date without us having to denormalise it onto job_card_v2.
def _jc_sort_expr(sort_col: str) -> str:
    if sort_col == "plan_date":
        return (
            "(SELECT plan_date FROM production_plan_v2 ppv "
            "WHERE ppv.plan_id = jc.plan_id)"
        )
    return f"jc.{sort_col}"
_JC_DATE_FIELDS    = frozenset({"created_at", "start_time", "end_time"})
_JC_PENDENCY_CHIPS = frozenset({"overdue", "due_today", "due_this_week", "future"})


async def list_job_cards(
    conn, *,
    entity: str | None = None,
    factory: str | None = None,
    floor: str | None = None,
    status: str | None = None,
    plan_id: int | None = None,
    so_number: str | None = None,
    machine_id: int | None = None,
    customer: str | None = None,
    search: str | None = None,
    date_field: str = "created_at",
    date_from: str | None = None,
    date_to:   str | None = None,
    pendency:  str | None = None,
    sort_by:   str = "created_at",
    sort_order: str = "DESC",
    page: int = 1,
    page_size: int = 100,
    user_scope_warehouses: list[str] | None = None,
    user_scope_floors:     list[str] | None = None,
) -> dict:
    """Paginated list of v2 job cards with the user-level lock applied
    when no explicit filter is given (admins should pass None for the
    scope kwargs to bypass).

    R3.D extensions:
      * sort_by + sort_order: pick the ORDER BY column at the call site
        rather than the hardcoded (plan_id DESC, plan_line_id, step). The
        sort column is validated against an allow-list to keep the SQL
        injection surface zero.
      * date_field + date_from / date_to: choose which date column the
        range filter applies to (created_at vs start_time vs end_time).
      * pendency: chip filter (overdue / due_today / due_this_week / future)
        computed against end_time as the proxy for stage deadline.
      * so_number, machine_id, plan_id: drill-down filters.
      * Counter block: top-line counters (total / locked / in_progress /
        completed / pending_issuance / overdue) returned alongside the
        page so the UI doesn't need a separate aggregate call.
    """
    conditions: list[str] = ["deleted_at IS NULL"]
    params: list = []
    idx = 1

    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if factory:
        # Tolerant match: 'A185' / 'A-185' / 'a 185' all collapse to the same canonical key.
        conditions.append(
            f"{WAREHOUSE_NORM_SQL('factory')} = {WAREHOUSE_NORM_SQL(f'${idx}')}"
        )
        params.append(factory); idx += 1
    elif user_scope_warehouses:
        conditions.append(WAREHOUSE_NORM_ANY_SQL("factory", f"${idx}"))
        params.append(list(user_scope_warehouses)); idx += 1
    if floor:
        conditions.append(f"floor = ${idx}"); params.append(floor); idx += 1
    elif user_scope_floors:
        conditions.append(f"floor = ANY(${idx}::text[])")
        params.append(list(user_scope_floors)); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',') if s.strip()]
        if statuses:
            ph = ', '.join(f'${idx + i}' for i in range(len(statuses)))
            conditions.append(f"status IN ({ph})")
            params.extend(statuses); idx += len(statuses)
    if plan_id is not None:
        conditions.append(f"plan_id = ${idx}"); params.append(plan_id); idx += 1
    if machine_id is not None:
        conditions.append(f"machine_id = ${idx}"); params.append(machine_id); idx += 1
    if so_number:
        # so_number is held on so_header; join via the plan-line ->
        # so_fulfillment_v2 -> so_line -> so_header chain that the SELECT
        # also uses. An EXISTS subquery keeps the outer plan size flat.
        conditions.append(f"""
            EXISTS (
                SELECT 1
                FROM   production_plan_line_v2 pl
                JOIN   so_fulfillment_v2 sf
                  ON   sf.so_fulfillment_id = ANY(pl.linked_so_fulfillment_ids)
                JOIN   so_line  sl ON sl.so_line_id = sf.so_line_id
                JOIN   so_header sh ON sh.so_id      = sl.so_id
                WHERE  pl.plan_line_id = jc.plan_line_id
                  AND  sh.so_number ILIKE ${idx}
            )
        """)
        params.append(f"%{so_number}%"); idx += 1
    if customer:
        # Multi-value support for parity with v1 (UI sends comma-separated).
        customers = [c.strip() for c in customer.split(',') if c.strip()]
        if customers:
            ph = ', '.join(f'${idx + i}' for i in range(len(customers)))
            conditions.append(f"customer_name = ANY(ARRAY[{ph}])")
            params.extend(customers); idx += len(customers)
    if search:
        # ILIKE across the columns the UI exposes in the search box. Mirrors
        # the v1 search semantics so the same query string lands the same hits.
        conditions.append(
            f"(job_card_number ILIKE ${idx} OR fg_sku_name ILIKE ${idx} "
            f"OR customer_name ILIKE ${idx} OR batch_number ILIKE ${idx})"
        )
        params.append(f"%{search}%"); idx += 1

    # Date range against the chosen date column. Allow-list keeps SQL
    # injection at bay - reject unknown date_field explicitly rather than
    # silently falling back to created_at (B10 H1 fix).
    if date_field not in _JC_DATE_FIELDS:
        return {
            "error": "invalid_date_field",
            "date_field": date_field,
            "allowed": sorted(_JC_DATE_FIELDS),
            "message": (
                f"date_field='{date_field}' is not recognised. "
                f"Allowed: {sorted(_JC_DATE_FIELDS)}."
            ),
        }
    date_col = date_field
    if date_from:
        conditions.append(f"{date_col} >= ${idx}::date"); params.append(date_from); idx += 1
    if date_to:
        conditions.append(f"{date_col} <= ${idx}::date"); params.append(date_to); idx += 1

    # Pendency chips - computed off production_plan_line_v2.deadline_date
    # joined via plan_line_id. The previous implementation keyed off
    # end_time (the JC COMPLETION timestamp), which is NULL while a JC
    # is open and set in the past once closed - mathematically incapable
    # of representing "due today" or "future". Migration 014 added the
    # real deadline column. B10 C1 fix.
    #
    # Build the pendency predicate separately so the counter block can
    # show meaningful chip totals over the non-pendency-filtered set
    # (B10 C2).
    pendency_predicate: str | None = None
    if pendency is not None and pendency not in _JC_PENDENCY_CHIPS:
        return {
            "error": "invalid_pendency",
            "pendency": pendency,
            "allowed": sorted(_JC_PENDENCY_CHIPS),
            "message": (
                f"pendency='{pendency}' is not a valid chip. "
                f"Allowed: {sorted(_JC_PENDENCY_CHIPS)}."
            ),
        }
    if pendency == "overdue":
        pendency_predicate = (
            "(SELECT pl.deadline_date FROM production_plan_line_v2 pl "
            "  WHERE pl.plan_line_id = jc.plan_line_id) < CURRENT_DATE "
            "AND jc.status NOT IN ('completed','closed','cancelled')"
        )
    elif pendency == "due_today":
        pendency_predicate = (
            "(SELECT pl.deadline_date FROM production_plan_line_v2 pl "
            "  WHERE pl.plan_line_id = jc.plan_line_id) = CURRENT_DATE "
            "AND jc.status NOT IN ('completed','closed','cancelled')"
        )
    elif pendency == "due_this_week":
        pendency_predicate = (
            "(SELECT pl.deadline_date FROM production_plan_line_v2 pl "
            "  WHERE pl.plan_line_id = jc.plan_line_id) "
            "  BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days' "
            "AND jc.status NOT IN ('completed','closed','cancelled')"
        )
    elif pendency == "future":
        pendency_predicate = (
            "(SELECT pl.deadline_date FROM production_plan_line_v2 pl "
            "  WHERE pl.plan_line_id = jc.plan_line_id) > CURRENT_DATE + INTERVAL '7 days'"
        )

    where_for_list = " AND ".join(conditions + ([pendency_predicate] if pendency_predicate else []))
    where_for_counters = " AND ".join(conditions)   # counter chips reflect the page set MINUS pendency / status

    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM job_card_v2 jc WHERE {where_for_list}", *params,
    )

    # Counter block: the WHERE deliberately OMITS the pendency filter so
    # chip-bar UIs can show how many JCs would fall into each chip when
    # the user switches. The status counters likewise reflect every
    # status in the unfiltered set. B10 C2 fix.
    counters_row = await conn.fetchrow(
        f"""
        SELECT
            COUNT(*)                                                AS total,
            COUNT(*) FILTER (WHERE status='locked')                 AS locked,
            COUNT(*) FILTER (WHERE status='in_progress')            AS in_progress,
            COUNT(*) FILTER (WHERE status='completed')              AS completed,
            COUNT(*) FILTER (WHERE status IN ('locked','unlocked')) AS pending_issuance,
            COUNT(*) FILTER (
                WHERE
                    (SELECT pl.deadline_date FROM production_plan_line_v2 pl
                       WHERE pl.plan_line_id = jc.plan_line_id) < CURRENT_DATE
                AND status NOT IN ('completed','closed','cancelled')
            )                                                       AS overdue
        FROM   job_card_v2 jc
        WHERE  {where_for_counters}
        """,
        *params,
    )
    where = where_for_list   # for the rest of this function (results query)

    # ORDER BY with allow-list. B10 H1: reject explicitly instead of
    # silently coercing to created_at - clients with a typo get an
    # immediate signal rather than wrong-ordered data.
    if sort_by not in _JC_SORTABLE_COLUMNS:
        return {
            "error": "invalid_sort_by",
            "sort_by": sort_by,
            "allowed": sorted(_JC_SORTABLE_COLUMNS),
            "message": (
                f"sort_by='{sort_by}' is not recognised. "
                f"Allowed: {sorted(_JC_SORTABLE_COLUMNS)}."
            ),
        }
    sort_col = sort_by
    sort_dir = "ASC" if (sort_order or "").upper() == "ASC" else "DESC"
    sort_expr = _jc_sort_expr(sort_col)
    # NULLS LAST so JCs without a linked plan_date (data anomaly) don't
    # float to the top under DESC sort. Tie-break on job_card_id keeps
    # the order stable when many JCs share the same plan_date.
    nulls = "NULLS LAST" if sort_dir == "DESC" else "NULLS FIRST"

    offset = (page - 1) * page_size
    rows = await conn.fetch(
        f"""
        SELECT jc.job_card_id, jc.job_card_number, jc.plan_id, jc.plan_line_id, jc.plan_step_id,
               jc.step_number, jc.process_name, jc.stage,
               jc.fg_sku_name, jc.customer_name, jc.batch_number,
               jc.planned_qty_kg, jc.planned_qty_units, jc.uom,
               jc.input_kind, jc.output_kind,
               jc.factory, jc.floor, jc.entity,
               jc.assigned_to_team_leader, jc.team_members,
               jc.is_locked, jc.locked_reason, jc.status,
               jc.start_time, jc.end_time, jc.total_time_min,
               jc.prev_job_card_id, jc.next_job_card_id,
               jc.carried_qty_kg, jc.dispatched_to_next_kg,
               jc.created_at,
               -- Planning date pulled from the parent plan. Separate from
               -- SO date (which the operator never wants to mutate). Used
               -- on the list page for sorting + the "Plan Date" column.
               (SELECT plan_date FROM production_plan_v2 ppv
                 WHERE ppv.plan_id = jc.plan_id) AS plan_date,
               -- Aggregated SO numbers for this JC's plan line. A plan line
               -- can fulfil multiple SOs (multi-SO bundling), so this is an
               -- array. ARRAY_AGG over the joined chain; NULL so_number rows
               -- are filtered upstream so the array never contains NULL.
               (SELECT ARRAY_AGG(DISTINCT sh.so_number)
                  FROM production_plan_line_v2 pl
                  JOIN so_fulfillment_v2 sf
                    ON sf.so_fulfillment_id = ANY(pl.linked_so_fulfillment_ids)
                  JOIN so_line  sl ON sl.so_line_id = sf.so_line_id
                  JOIN so_header sh ON sh.so_id      = sl.so_id
                 WHERE pl.plan_line_id = jc.plan_line_id
                   AND sh.so_number IS NOT NULL) AS so_numbers
        FROM job_card_v2 jc
        WHERE {where}
        ORDER BY {sort_expr} {sort_dir} {nulls}, jc.job_card_id {sort_dir}
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params, page_size, offset,
    )

    return {
        "results": [_serialize(r) for r in rows],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total or 0,
            "total_pages": ((total or 0) + page_size - 1) // page_size if total else 0,
        },
        "counters": {
            "total":            counters_row["total"]            or 0,
            "locked":           counters_row["locked"]           or 0,
            "in_progress":      counters_row["in_progress"]      or 0,
            "completed":        counters_row["completed"]        or 0,
            "pending_issuance": counters_row["pending_issuance"] or 0,
            "overdue":          counters_row["overdue"]          or 0,
        },
        "sort":   {"sort_by": sort_col, "sort_order": sort_dir},
    }


# ---------------------------------------------------------------------------
# Free-text search (unpaginated)
# ---------------------------------------------------------------------------
#
# Distinct from list_job_cards in two ways:
#   1. No page / page_size — the endpoint deliberately drops pagination so
#      callers can do "find anything matching this string" without juggling
#      page counts. A hard cap (`SEARCH_HARD_CAP`) prevents pathological
#      queries from materialising the whole table.
#   2. Search reach is wider — every user-visible identifier is matched,
#      including plan_id (cast to text) and the SO number via a join across
#      production_plan_line_v2 → so_fulfillment_v2 → so_line → so_header.
#
# Case-insensitivity is implemented as LOWER(col) LIKE LOWER($needle) on both
# sides of the comparison, per spec. We deliberately do NOT add a lowercase
# mirror column to the schema — the cost of LOWER() in a one-off search is
# acceptable, and it keeps the canonical row identical to what the writer
# inserted.

SEARCH_HARD_CAP = 1000


async def search_job_cards(
    conn, *,
    q: str | None = None,
    status: str | None = None,
    entity: str | None = None,
    factory: str | None = None,
    floor: str | None = None,
    user_scope_warehouses: list[str] | None = None,
    user_scope_floors:     list[str] | None = None,
) -> dict:
    conditions: list[str] = ["deleted_at IS NULL"]
    params: list = []
    idx = 1

    # Same scope-lock semantics as list_job_cards — explicit factory/floor
    # are validated upstream in the router; the implicit user-scope intersect
    # applies here when no explicit param was given.
    if entity:
        conditions.append(f"entity = ${idx}"); params.append(entity); idx += 1
    if factory:
        conditions.append(
            f"{WAREHOUSE_NORM_SQL('factory')} = {WAREHOUSE_NORM_SQL(f'${idx}')}"
        )
        params.append(factory); idx += 1
    elif user_scope_warehouses:
        conditions.append(WAREHOUSE_NORM_ANY_SQL("factory", f"${idx}"))
        params.append(list(user_scope_warehouses)); idx += 1
    if floor:
        conditions.append(f"floor = ${idx}"); params.append(floor); idx += 1
    elif user_scope_floors:
        conditions.append(f"floor = ANY(${idx}::text[])")
        params.append(list(user_scope_floors)); idx += 1
    if status:
        statuses = [s.strip() for s in status.split(',') if s.strip()]
        if statuses:
            ph = ', '.join(f'${idx + i}' for i in range(len(statuses)))
            conditions.append(f"status IN ({ph})")
            params.extend(statuses); idx += len(statuses)

    if q:
        # Bind the needle exactly once — the same parameter feeds every OR
        # branch in the search clause. LOWER() applied to both sides per spec
        # ("convert both side values to lowercase in the backend before
        # comparing"); no lowercase-mirror column is kept on the row.
        needle = f"%{q.lower()}%"
        text_cols = [
            "job_card_number", "fg_sku_name", "customer_name",
            "batch_number", "process_name", "stage",
            "assigned_to_team_leader", "factory", "floor", "entity",
        ]
        clauses = [f"LOWER({c}) LIKE ${idx}" for c in text_cols]
        # plan_id is BIGINT — cast so users can paste a plan number directly.
        clauses.append(f"LOWER(CAST(plan_id AS TEXT)) LIKE ${idx}")
        # job_card_id likewise (sometimes operators only know the short id).
        clauses.append(f"LOWER(CAST(job_card_id AS TEXT)) LIKE ${idx}")
        # SO number via the plan→fulfillment→so_line→so_header chain.
        # Aliased as `jc` in the outer FROM, so this subquery references jc.
        clauses.append(
            "EXISTS (SELECT 1 "
            "FROM production_plan_line_v2 pl "
            "JOIN so_fulfillment_v2 sf "
            "  ON sf.so_fulfillment_id = ANY(pl.linked_so_fulfillment_ids) "
            "JOIN so_line sl ON sl.so_line_id = sf.so_line_id "
            "JOIN so_header sh ON sh.so_id = sl.so_id "
            f"WHERE pl.plan_id = jc.plan_id "
            f"  AND LOWER(sh.so_number) LIKE ${idx})"
        )
        conditions.append("(" + " OR ".join(clauses) + ")")
        params.append(needle); idx += 1

    where = " AND ".join(conditions)
    rows = await conn.fetch(
        f"""
        SELECT jc.job_card_id, jc.job_card_number, jc.plan_id, jc.plan_line_id, jc.plan_step_id,
               jc.step_number, jc.process_name, jc.stage,
               jc.fg_sku_name, jc.customer_name, jc.batch_number,
               jc.planned_qty_kg, jc.planned_qty_units, jc.uom,
               jc.input_kind, jc.output_kind,
               jc.factory, jc.floor, jc.entity,
               jc.assigned_to_team_leader, jc.team_members,
               jc.is_locked, jc.locked_reason, jc.status,
               jc.start_time, jc.end_time, jc.total_time_min,
               jc.prev_job_card_id, jc.next_job_card_id,
               jc.carried_qty_kg, jc.dispatched_to_next_kg,
               jc.created_at,
               (SELECT ARRAY_AGG(DISTINCT sh.so_number)
                  FROM production_plan_line_v2 pl
                  JOIN so_fulfillment_v2 sf
                    ON sf.so_fulfillment_id = ANY(pl.linked_so_fulfillment_ids)
                  JOIN so_line  sl ON sl.so_line_id = sf.so_line_id
                  JOIN so_header sh ON sh.so_id      = sl.so_id
                 WHERE pl.plan_line_id = jc.plan_line_id
                   AND sh.so_number IS NOT NULL) AS so_numbers
        FROM job_card_v2 jc
        WHERE {where}
        ORDER BY jc.created_at DESC
        LIMIT {SEARCH_HARD_CAP + 1}
        """,
        *params,
    )
    # Fetch one extra so we can detect cap hits without a separate COUNT.
    capped = len(rows) > SEARCH_HARD_CAP
    if capped:
        rows = rows[:SEARCH_HARD_CAP]
    return {
        "results": [_serialize(r) for r in rows],
        "total": len(rows),
        "capped": capped,
        "hard_cap": SEARCH_HARD_CAP,
    }


# ---------------------------------------------------------------------------
# Team assignment
# ---------------------------------------------------------------------------

async def assign_team(conn, *, job_card_id: int,
                      team_leader: str,
                      team_members: list[str] | None = None) -> dict:
    """Set the assigned team leader and team members on a v2 JC.

    Side effects:
        - Writes assigned_to_team_leader + team_members[].
        - If the JC was 'unlocked', moves it to 'assigned' so the floor
          team_leader's queue picks it up.
        - 'locked' / 'closed' / 'cancelled' JCs cannot be assigned;
          returns an error so the caller can show a clear message.
    """
    if not team_leader or not team_leader.strip():
        return {"error": "missing_team_leader",
                "message": "team_leader is required"}

    jc = await conn.fetchrow(
        "SELECT status FROM job_card_v2 WHERE job_card_id=$1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] in ('closed', 'cancelled'):
        return {"error": "terminal_state", "current_status": jc["status"]}
    if jc["status"] == 'locked':
        return {"error": "locked",
                "message": "JC is locked. Wait for the previous stage to dispatch material first."}

    # Clean + dedupe team_members case-insensitively. None / [] both mean
    # "no members — solo team leader run"; we store an empty array rather
    # than NULL so the column reads consistently.
    cleaned: list[str] = []
    if team_members:
        seen_lower: set[str] = set()
        for raw in team_members:
            if raw is None:
                continue
            t = str(raw).strip()
            if not t:
                continue
            if t.lower() in seen_lower:
                continue
            seen_lower.add(t.lower())
            cleaned.append(t)

    # Transition unlocked → assigned on first assignment. Re-assigning an
    # already-assigned card just updates the names without moving status.
    next_status = 'assigned' if jc["status"] == 'unlocked' else jc["status"]

    row = await conn.fetchrow(
        """
        UPDATE job_card_v2
           SET assigned_to_team_leader = $2,
               team_members            = $3,
               status                  = $4
         WHERE job_card_id = $1
        RETURNING *
        """,
        job_card_id, team_leader.strip(), cleaned, next_status,
    )
    return {
        "assigned": True,
        "job_card": _serialize(row),
        "team_leader": team_leader.strip(),
        "team_members": cleaned,
    }


# ---------------------------------------------------------------------------
# Stage handoff: dispatch WIP/SFG to the next stage's JC
# ---------------------------------------------------------------------------

async def dispatch_to_next(conn, *, job_card_id: int, qty_kg: float,
                           qty_units: float | None = None,
                           dispatched_by: str | None = None,
                           notes: str | None = None) -> dict:
    """Hand off material from this JC to its next_job_card_id partner.

    Side effects (single transaction):
        1. Insert a row in job_card_partial_dispatch_v2 (audit trail).
        2. Increment this JC's dispatched_to_next_kg by qty_kg.
        3. Increment the next JC's carried_qty_kg by qty_kg.
        4. If the next JC is locked with reason='awaiting_previous_stage',
           unlock it (status='unlocked', is_locked=false, locked_reason=NULL)
           so the downstream floor can start work on the received material.
    """
    if qty_kg <= 0:
        return {"error": "invalid_qty", "message": "qty_kg must be > 0"}

    src = await conn.fetchrow(
        """
        SELECT job_card_id, next_job_card_id, planned_qty_kg,
               dispatched_to_next_kg, output_kind
        FROM   job_card_v2
        WHERE  job_card_id=$1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not src:
        return {"error": "job_card_not_found"}
    if src["output_kind"] == 'FG':
        return {"error": "no_next_stage",
                "message": "Last stage in the chain has no downstream JC"}
    if src["next_job_card_id"] is None:
        return {"error": "chain_broken",
                "message": "next_job_card_id is NULL; cannot dispatch"}

    async def _insert_dispatch():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_partial_dispatch_v2
                (dispatch_id, from_job_card_id, to_job_card_id, qty_kg, qty_units,
                 dispatched_by, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, src["next_job_card_id"], qty_kg, qty_units,
            dispatched_by, notes,
        )
    audit = await insert_with_pk_retry(conn, _insert_dispatch)
    await conn.execute(
        "UPDATE job_card_v2 SET dispatched_to_next_kg = dispatched_to_next_kg + $1 WHERE job_card_id=$2",
        qty_kg, job_card_id,
    )
    await conn.execute(
        """
        UPDATE job_card_v2
           SET carried_qty_kg = carried_qty_kg + $1,
               is_locked      = CASE WHEN locked_reason = 'awaiting_previous_stage' THEN FALSE ELSE is_locked END,
               locked_reason  = CASE WHEN locked_reason = 'awaiting_previous_stage' THEN NULL  ELSE locked_reason END,
               status         = CASE
                                  WHEN status = 'locked' AND locked_reason = 'awaiting_previous_stage'
                                       THEN 'unlocked'
                                  ELSE status
                                END
         WHERE job_card_id=$2
        """,
        qty_kg, src["next_job_card_id"],
    )
    return {"dispatched": True, "dispatch": _serialize(audit)}


# ---------------------------------------------------------------------------
# Output capture
# ---------------------------------------------------------------------------

async def record_output(conn, *, job_card_id: int,
                        rm_consumed_kg: float, output_qty_kg: float,
                        output_qty_units: float | None = None,
                        output_kind: str | None = None,
                        uom: str | None = None,
                        notes: str | None = None,
                        process_loss_kg: float | None = None,
                        recorded_by: str | None = None) -> dict:
    """Append an output row for this JC. The output_kind defaults to the
    JC's declared output_kind (SFG / WIP / FG from the stage chain) unless
    overridden — e.g. when the floor reports a partial FG batch that's
    actually still WIP because QC failed.

    yield_pct is computed server-side as (output / rm_consumed) × 100 when
    rm_consumed > 0; NULL otherwise. The JC's status is NOT auto-flipped
    here — explicit /complete and /close calls handle that.
    """
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    # Validate + normalize inputs and compute yield. rm_consumed_kg is
    # optional: a packaging / later stage records FG output (and PM
    # consumption) without an RM figure, so a missing value normalizes to 0
    # and yield is left uncomputed. The implausible-yield guard (NUMERIC(6,3)
    # overflow / unit-typo protection) lives in compute_output_row.
    calc = compute_output_row(output_qty_kg, rm_consumed_kg)
    if "error" in calc:
        return calc
    rm_consumed_kg = calc["rm_consumed_kg"]
    yield_pct = calc["yield_pct"]

    jc = await conn.fetchrow(
        "SELECT job_card_id, output_kind FROM job_card_v2 WHERE job_card_id=$1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}

    kind = output_kind or jc["output_kind"]

    async def _insert_output():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_output_v2
                (output_id, job_card_id, rm_consumed_kg, output_qty_kg, output_qty_units,
                 output_kind, uom, yield_pct, notes, recorded_by, process_loss_kg)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, rm_consumed_kg, output_qty_kg, output_qty_units,
            kind, uom, yield_pct, notes, recorded_by, process_loss_kg or 0,
        )
    inserted = await insert_with_pk_retry(conn, _insert_output)
    return {"recorded": True, "output": _serialize(inserted), "yield_pct": yield_pct}


# ---------------------------------------------------------------------------
# Balance materials (job_card_balance_material_v2)
# ---------------------------------------------------------------------------

VALID_BALANCE_TYPES = ('extra_given', 'returned', 'wastage', 'control_sample')

# Consolidated-EGA sentinel: operationally, nobody can attribute EGA to a
# specific RM after the batch is run, so the UI submits ONE EGA row with
# this sentinel as material_name (bom_line_id=NULL). The item-type gate
# below skips when this token is present — the packing-stage gate stays.
EGA_CONSOLIDATED_SENTINEL = 'CONSOLIDATED'


async def replace_balance_materials(conn, *, job_card_id: int,
                                    rows: list[dict],
                                    recorded_by: str | None = None,
                                    batch_id: int | None = None) -> dict:
    """Replace this JC's balance material rows wholesale (delete-then-
    insert). The Android Output form sends one entry per BOM article on
    every save with `qty_kg = 0` meaning "explicitly no leftover for
    this article" (not "skip"). Re-saving zero needs to clear a prior
    non-zero value, so we can't filter zeros server-side — and we can't
    rely on an UPSERT keyed on bom_line_id either because the
    `extra_given` row's bom_line_id can be NULL (the operator picks the
    article via a spinner) and NULL columns don't conflict in PostgreSQL.

    R11 EGA validation (B6): rows with balance_type='extra_given' must
        (a) point at a BOM material whose item_type='rm' (not 'pm') and
        (b) be on a packing-stage JC. Both gated at the top so we don't
        delete the existing rows on a doomed save.

    R6 lock guard (B6 H1 fix): blocks the wholesale replace on a locked
    JC. Other balance-material entry paths already gate; this one was
    missing the check.

    Wholesale replace matches the v1 engine's pattern
    (job_card_engine.record_output_v2). Returns the inserted rows so the
    response can echo back what was saved."""
    # B6 H1: lock guard.
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err

    saved: list[dict] = []
    has_ega = False
    for r in rows:
        balance_type = r.get("balance_type")
        if balance_type not in VALID_BALANCE_TYPES:
            return {"error": "invalid_balance_type", "balance_type": balance_type}
        qty = float(r.get("qty_kg") or 0)
        if qty < 0:
            return {"error": "negative_qty", "balance_type": balance_type}
        material_name = r.get("material_name") or r.get("material_sku_name")
        if not material_name:
            return {"error": "missing_material_name", "balance_type": balance_type}
        if balance_type == 'extra_given' and qty > 0:
            has_ega = True

    # R11: EGA-specific gates (only run if at least one extra_given row has
    # qty > 0 — saving zero EGA is a no-op and shouldn't be blocked).
    if has_ega:
        jc_meta = await conn.fetchrow(
            "SELECT bom_id, stage FROM job_card_v2 "
            "WHERE  job_card_id=$1 AND deleted_at IS NULL "
            "FOR    UPDATE",
            job_card_id,
        )
        if jc_meta is None:
            return {"error": "job_card_not_found"}
        # B6 C2 fix: substring match (normalised) instead of exact-string.
        if not is_packing_stage(jc_meta["stage"]):
            return {
                "error": "ega_non_packing_stage",
                "stage": jc_meta["stage"],
                "message": (
                    f"EGA can only be recorded on packing stages "
                    f"(got '{jc_meta['stage']}')."
                ),
            }
        # B6 H2/H3 fix: prefer bom_line_id when the row carries it; only
        # fall back to fuzzy name match otherwise. ORDER BY line_number on
        # the name fallback so the choice is deterministic if multiple
        # rows share the same material name. Affirmatively require 'rm'
        # rather than just rejecting 'pm' (M2 from the review).
        for r in rows:
            if r.get("balance_type") != 'extra_given':
                continue
            qty = float(r.get("qty_kg") or 0)
            if qty == 0:
                continue
            # Consolidated EGA — operator-stated: per-article attribution
            # is unknowable post-run. The packing-stage gate above still
            # ran; the per-material item_type gate is skipped here.
            material_name = (r.get("material_name") or r.get("material_sku_name") or "").strip()
            if material_name.upper() == EGA_CONSOLIDATED_SENTINEL:
                continue
            bom_line_id = r.get("bom_line_id")
            if bom_line_id:
                item_type = await conn.fetchval(
                    "SELECT item_type FROM bom_line WHERE bom_line_id=$1",
                    bom_line_id,
                )
                lookup_key = f"bom_line_id={bom_line_id}"
            else:
                material_name = r.get("material_name") or r.get("material_sku_name")
                item_type = await conn.fetchval(
                    "SELECT item_type FROM bom_line "
                    "WHERE  bom_id=$1 AND material_sku_name=$2 "
                    "ORDER  BY line_number LIMIT 1",
                    jc_meta["bom_id"], material_name,
                )
                lookup_key = f"material='{material_name}'"
            if item_type is None:
                return {
                    "error": "ega_material_not_in_bom",
                    "lookup": lookup_key,
                    "message": (
                        f"EGA material ({lookup_key}) is not in the "
                        "JC's BOM line items."
                    ),
                }
            if item_type.strip().lower() != 'rm':
                return {
                    "error": "ega_non_rm_material",
                    "lookup": lookup_key,
                    "item_type": item_type,
                    "message": (
                        f"EGA refused for material ({lookup_key}) with "
                        f"item_type='{item_type}' - EGA is only valid for RM."
                    ),
                }

    # Stage 2: scope the wholesale delete to the SAME batch we're about
    # to write into.  Two batches on the same JC can carry independent
    # balance-material sets.  When batch_id is None (legacy path), we
    # delete only the rows ALSO tagged NULL so existing batched data is
    # preserved.
    if batch_id is None:
        await conn.execute(
            "DELETE FROM job_card_balance_material_v2 "
            "WHERE job_card_id = $1 AND batch_id IS NULL",
            job_card_id,
        )
    else:
        await conn.execute(
            "DELETE FROM job_card_balance_material_v2 "
            "WHERE job_card_id = $1 AND batch_id = $2",
            job_card_id, batch_id,
        )
    for r in rows:
        async def _insert(_r=r, _batch=batch_id):
            return await conn.fetchrow(
                """
                INSERT INTO job_card_balance_material_v2 (
                    balance_id, job_card_id, batch_id, bom_line_id,
                    material_id, material_name, balance_type, qty_kg,
                    remarks, recorded_by
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
                """,
                new_short_time_id(),
                job_card_id,
                _batch,
                _r.get("bom_line_id"),
                _r.get("material_id"),
                _r.get("material_name") or _r.get("material_sku_name"),
                _r["balance_type"],
                float(_r.get("qty_kg") or 0),
                _r.get("remarks"),
                recorded_by,
            )
        row = await insert_with_pk_retry(conn, _insert)
        saved.append(_serialize(row))
    return {"saved": True, "rows": saved}


# ---------------------------------------------------------------------------
# QC summary (job_card_qc_v2)
# ---------------------------------------------------------------------------

async def upsert_qc(conn, *, job_card_id: int,
                    passed: bool | None,
                    findings: str | None = None,
                    corrective_action: str | None = None,
                    inspector_user: str | None = None,
                    recorded_by: str | None = None) -> dict:
    """Persist a single-row QC summary. `passed` maps to result:
    True → 'pass', False → 'fail', None → 'pending'. Re-saves UPDATE the
    same row (UNIQUE on job_card_id); inspection_date is stamped to NOW
    on every save so the timestamp reflects the latest call."""
    result = 'pending' if passed is None else ('pass' if passed else 'fail')

    async def _upsert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_qc_v2 (
                qc_id, job_card_id, result, findings, corrective_action,
                inspector_user, inspection_date, recorded_by
            )
            VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7)
            ON CONFLICT (job_card_id) DO UPDATE SET
                result            = EXCLUDED.result,
                findings          = EXCLUDED.findings,
                corrective_action = EXCLUDED.corrective_action,
                inspector_user    = EXCLUDED.inspector_user,
                inspection_date   = NOW(),
                recorded_by       = EXCLUDED.recorded_by
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, result, findings, corrective_action,
            inspector_user, recorded_by,
        )
    row = await insert_with_pk_retry(conn, _upsert)
    return {"saved": True, "qc": _serialize(row)}


# ---------------------------------------------------------------------------
# Sign-off
# ---------------------------------------------------------------------------

async def add_sign_off(conn, *, job_card_id: int, role: str,
                       signed_by: str, notes: str | None = None) -> dict:
    """Record a sign-off. UNIQUE (job_card_id, role) — re-signing under
    the same role updates the row's signer + signed_at rather than
    inserting a duplicate."""
    if not role:
        return {"error": "missing_role"}
    if not signed_by:
        return {"error": "missing_signer"}
    async def _insert_sign_off():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_sign_off_v2 (sign_off_id, job_card_id, role, signed_by, notes)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (job_card_id, role)
            DO UPDATE SET signed_by = EXCLUDED.signed_by,
                          signed_at = NOW(),
                          notes     = EXCLUDED.notes
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, role, signed_by, notes,
        )
    row = await insert_with_pk_retry(conn, _insert_sign_off)
    return {"signed": True, "sign_off": _serialize(row)}


# ---------------------------------------------------------------------------
# Lifecycle transitions: start / complete / force-unlock / patch / cancel
# ---------------------------------------------------------------------------

async def start_job_card(conn, *, job_card_id: int) -> dict:
    """Move a v2 JC into 'in_progress'.

    Transition rules:
        locked        → refused; the prev stage hasn't dispatched material
        unlocked      → OK (e.g. stage-1 RM not yet received but supervisor
                            says go ahead — same relaxed gating as v1)
        assigned      → OK
        material_received → OK
        in_progress / completed / closed / cancelled → refused (idempotent
                            "already there" or terminal state)
    """
    # R6 lock guard owns the 'locked' rejection. assert_not_locked sees
    # is_locked=TRUE (which always coincides with status='locked' on
    # well-formed rows) and returns the error dict; force_unlock flips
    # both is_locked AND status='unlocked' in the same txn so a JC with
    # status='locked' AND force_unlocked=TRUE is structurally impossible.
    # Result: no separate status='locked' branch needed below.
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err
    jc = await conn.fetchrow(
        "SELECT status, start_time FROM job_card_v2 WHERE job_card_id=$1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] in ('in_progress', 'completed', 'closed', 'cancelled'):
        return {"error": "invalid_status",
                "message": f"Cannot start a JC in status '{jc['status']}'"}

    # Stamp start_time only if not already set — re-starts (which shouldn't
    # happen given the guards above) would otherwise erase the original.
    row = await conn.fetchrow(
        """
        UPDATE job_card_v2
           SET status     = 'in_progress',
               start_time = COALESCE(start_time, NOW())
         WHERE job_card_id = $1
        RETURNING status, start_time
        """,
        job_card_id,
    )
    return {
        "started":     True,
        "job_card_id": job_card_id,
        "status":      row["status"],
        "start_time":  row["start_time"].isoformat() if row["start_time"] else None,
    }


async def _derive_accounting_payload(conn, job_card_id: int) -> dict:
    """Reconstruct an accounting-summary payload from the persisted raw
    data for this JC, so complete_job_card can self-heal when the
    operator hits Complete before Save Output has fired the explicit
    PUT /accounting/summary.

    Mirrors the formulas the web SummaryCard uses byte-for-byte (RM-
    only consumption denominator, wastage folded into Process Loss for
    display but kept separate in the persisted columns, off-grade and
    rejection collapsed into one bucket per operator policy).

    Returns a dict in the AccountingSummaryRequest shape (router.py:5185).
    Used only as a fallback — when the operator goes through the normal
    Save Output → /complete flow with a recent client build, the
    explicit PUT /accounting/summary has already run and this code path
    is skipped.
    """
    # Latest output row carries fg_actual_kg/units + process_loss_kg.
    out_row = await conn.fetchrow(
        """
        SELECT output_qty_kg, output_qty_units, process_loss_kg
        FROM   job_card_output_v2
        WHERE  job_card_id = $1
        ORDER  BY recorded_at DESC
        LIMIT  1
        """,
        job_card_id,
    )
    output_qty       = float(out_row["output_qty_kg"] or 0) if out_row else 0.0
    output_qty_units = (float(out_row["output_qty_units"])
                       if out_row and out_row["output_qty_units"] is not None
                       else None)
    process_loss_kg  = float(out_row["process_loss_kg"] or 0) if out_row else 0.0

    # total_input: prefer canonical rm_issued + carried_in; fall back to
    # RM-only consumption sum (PM rows excluded since packaging doesn't
    # convert into FG mass).
    rm_issued = float(await conn.fetchval(
        """
        SELECT COALESCE(SUM(issued_qty), 0)
        FROM   job_card_rm_indent_v2
        WHERE  job_card_id = $1
        """,
        job_card_id,
    ) or 0)
    carried_in = float(await conn.fetchval(
        "SELECT carried_qty_kg FROM job_card_v2 WHERE job_card_id = $1",
        job_card_id,
    ) or 0)
    canonical_input = rm_issued + carried_in
    if canonical_input <= 0:
        # RM-only consumption sum. input_kind='RM' filter excludes the
        # PM rows the operator typed alongside in the same grid.
        rm_consumption = float(await conn.fetchval(
            """
            SELECT COALESCE(SUM(actual_consumed_qty), 0)
            FROM   job_card_material_consumption_v2
            WHERE  job_card_id = $1
              AND  COALESCE(input_kind, 'RM') = 'RM'
            """,
            job_card_id,
        ) or 0)
        total_input = rm_consumption
    else:
        total_input = canonical_input

    # Byproducts roll up: off-grade is everything except control_sample,
    # balance_material, pm_*, and wastage. Wastage stays separate; the
    # display aggregates it into Process Loss but the persisted column
    # is its own bucket so the conservation identity stays clean.
    bp_rows = await conn.fetch(
        """
        SELECT category, COALESCE(SUM(quantity), 0) AS qty
        FROM   job_card_byproducts_v2
        WHERE  job_card_id = $1
        GROUP  BY category
        """,
        job_card_id,
    )
    offgrade_total = 0.0
    wastage        = 0.0
    control_sample = 0.0
    for r in bp_rows:
        cat = (r["category"] or "").strip()
        qty = float(r["qty"] or 0)
        if cat == "control_sample":
            control_sample += qty
        elif cat == "wastage":
            wastage += qty
        elif cat == "balance_material":
            # balance_material on byproducts is legacy; the canonical
            # path uses balance_materials with balance_type='returned'.
            # Don't double-count.
            pass
        elif cat.startswith("pm_"):
            # PM variance is its own track (pm_variance_breakdown JSONB),
            # not part of the kg conservation identity.
            pass
        else:
            offgrade_total += qty

    # balance_materials roll up: 'returned' is the per-line leftover; the
    # 'extra_given' (consolidated EGA) qty is summed separately.
    bm_rows = await conn.fetch(
        """
        SELECT balance_type, COALESCE(SUM(qty_kg), 0) AS qty
        FROM   job_card_balance_material_v2
        WHERE  job_card_id = $1
        GROUP  BY balance_type
        """,
        job_card_id,
    )
    balance_material_qty = 0.0
    extra_give_away      = 0.0
    for r in bm_rows:
        bt  = (r["balance_type"] or "").strip()
        qty = float(r["qty"] or 0)
        if bt == "returned":
            balance_material_qty += qty
        elif bt == "extra_given":
            extra_give_away += qty

    return {
        "total_input_qty":      total_input,
        "input_uom":             "KGS",
        "output_qty":            output_qty,
        "output_uom":            "KGS",
        "output_qty_units":      output_qty_units,
        "process_loss_qty":      process_loss_kg,
        "extra_give_away_qty":   extra_give_away,
        "balance_material_qty":  balance_material_qty,
        "offgrade_total_qty":    offgrade_total,
        "rejection_qty":         0.0,      # one bucket with off-grade per op policy
        "wastage_qty":           wastage,
        "control_sample_qty":    control_sample,
    }


async def complete_job_card(conn, *, job_card_id: int,
                             force: bool = False,
                             request_id: int | None = None,
                             completed_by: str | None = None) -> dict:
    """Move a v2 JC from 'in_progress' to 'completed'.

    Side effects:
        - Stamps end_time = NOW().
        - total_time_min: if any shift segments exist, leave the running
          roll-up alone (it's already the source of truth). Otherwise
          compute end_time - start_time as a fallback for JCs that didn't
          use multi-shift capture.

    R9 closure gate:
        Reads job_card_accounting_v2.is_balanced. When False, refuses with
        {"error": "unbalanced", ...} unless the caller supplies BOTH
        force=True AND a request_id - an R8 maker-checker amendment of
        type 'unbalanced_close_override' that has been approved. The
        approved-status check on the request_id is enforced at the
        amendment service layer (B11); until B11 ships, the request_id
        is accepted as an audit-trail token only.

    R13 closure gate:
        Refuses if any batch row for this JC is still 'open'. Batches must
        be closed (or cancelled) before /complete can run.

    NOTE: v2 does NOT auto-unlock the next stage on complete. The dispatch
    flow (POST /dispatch-to-next) handles that — handing off material to
    the downstream JC is what makes "I finished" actionable downstream.
    Calling complete without dispatching means "I'm done at this stage
    but no material has flowed yet"; the next stage stays locked.
    """
    jc = await conn.fetchrow(
        """
        SELECT status, start_time, total_time_min
        FROM   job_card_v2
        WHERE  job_card_id = $1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] != 'in_progress':
        return {"error": "invalid_status",
                "message": f"Can only complete in_progress JCs (currently '{jc['status']}')"}

    open_shifts = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_shift_log_v2 WHERE job_card_id=$1 AND end_at IS NULL",
        job_card_id,
    )
    if open_shifts:
        return {"error": "open_shift",
                "message": "Stop the open shift segment before completing"}

    # ── R13: refuse if any batch is still open.
    open_batch = await conn.fetchrow(
        """
        SELECT batch_id, batch_number, batch_date
        FROM   job_card_batch_v2
        WHERE  job_card_id = $1 AND status = 'open'
        LIMIT  1
        """,
        job_card_id,
    )
    if open_batch:
        return {
            "error":        "open_batch",
            "batch_id":     open_batch["batch_id"],
            "batch_number": open_batch["batch_number"],
            "batch_date":   open_batch["batch_date"].isoformat() if open_batch["batch_date"] else None,
            "message": (
                f"Batch {open_batch['batch_number']} "
                f"({open_batch['batch_date']}) is still open. Close it "
                "before completing the JC."
            ),
        }

    # ── R9: reject when accounting is unbalanced unless caller force-
    # overrides with an R8-approved request_id. is_balanced is the
    # canonical truth from the most recent accounting summary save (B4).
    # If no accounting row exists yet, treat as unbalanced (operator
    # must save accounting before close).
    # FOR UPDATE locks the accounting row so a concurrent B4 save can't
    # flip is_balanced between this read and the JC status UPDATE below.
    acct = await conn.fetchrow(
        """
        SELECT is_balanced, balance_difference_qty
        FROM   job_card_accounting_v2
        WHERE  job_card_id = $1
        FOR    UPDATE
        """,
        job_card_id,
    )
    if acct is None:
        # Self-heal: derive an accounting payload from the persisted raw
        # data (outputs + consumption + byproducts + balance_materials)
        # and run save_accounting inline. Otherwise the operator gets a
        # confusing 400 ("Accounting summary not saved yet") even after
        # a successful Save Output — because /outputs doesn't fire
        # /accounting/summary as a side effect, and older client builds
        # don't fire it explicitly. With this fallback, /complete always
        # has an accounting row to read.
        from app.modules.production.services.jc_accounting_v2 import save_accounting
        derived = await _derive_accounting_payload(conn, job_card_id)
        save_result = await save_accounting(
            conn,
            job_card_id=job_card_id,
            payload=derived,
            saved_by=completed_by,
        )
        if save_result.get("error"):
            return {
                "error": "accounting_save_failed",
                "message": (
                    "Couldn't auto-derive an accounting summary: "
                    f"{save_result.get('message') or save_result.get('error')}. "
                    "Save the summary manually before completing."
                ),
                "underlying": save_result,
            }
        # Re-read the now-saved row with FOR UPDATE so the rest of the
        # gate runs on a stable snapshot.
        acct = await conn.fetchrow(
            """
            SELECT is_balanced, balance_difference_qty
            FROM   job_card_accounting_v2
            WHERE  job_card_id = $1
            FOR    UPDATE
            """,
            job_card_id,
        )
        if acct is None:
            return {
                "error": "no_accounting",
                "message": (
                    "Auto-derive succeeded but no accounting row materialised "
                    "(unexpected). Save the summary manually."
                ),
            }

    # If unbalanced, the only path forward is an R8 amendment override.
    # C1 fix: the request_id has to actually exist, point at this JC, and
    # be the right type. B11 will add the status='approved' enforcement;
    # for now we at least validate the row IS what it claims to be so a
    # random integer can't bypass the gate.
    override_valid = False
    if force and request_id is not None:
        amendment = await conn.fetchrow(
            """
            SELECT request_id, request_type, status
            FROM   bom_amendment_request_v2
            WHERE  request_id  = $1
              AND  job_card_id = $2
            """,
            request_id, job_card_id,
        )
        if amendment is None:
            return {
                "error": "override_request_not_found",
                "request_id": request_id,
                "message": (
                    f"No bom_amendment_request_v2 row with request_id={request_id} "
                    f"references this JC. File the unbalanced_close_override "
                    "amendment first."
                ),
            }
        if amendment["request_type"] != 'unbalanced_close_override':
            return {
                "error": "override_request_wrong_type",
                "request_id": request_id,
                "request_type": amendment["request_type"],
                "message": (
                    "Override request_id must point at an "
                    "'unbalanced_close_override' amendment; got "
                    f"'{amendment['request_type']}'."
                ),
            }
        # B11 closes the loop: require the amendment to have advanced
        # through the maker-checker chain. 'approved' or 'applied' both
        # mean the checker(s) signed off; 'applied' just means the
        # apply step already noop-completed for this request_type.
        if amendment["status"] not in ('approved', 'applied'):
            return {
                "error": "override_request_not_approved",
                "request_id": request_id,
                "status":     amendment["status"],
                "message": (
                    f"Override request_id={request_id} is in status "
                    f"'{amendment['status']}' - awaiting approval. "
                    "Cannot be used to force-close until status='approved' "
                    "or 'applied'."
                ),
            }
        override_valid = True

    if not acct["is_balanced"] and not override_valid:
        return {
            "error": "unbalanced",
            "balance_difference_qty": float(acct["balance_difference_qty"]),
            "message": (
                "Accounting is unbalanced. Resolve the variance OR file an "
                "'unbalanced_close_override' amendment (R8 row 12) and "
                "retry with force=true and the approved request_id."
            ),
        }

    # Fallback total_time_min only when shift log didn't populate it.
    use_fallback = (jc["total_time_min"] is None or float(jc["total_time_min"]) == 0)
    # B5 C2 audit trail: stamp force_closed + request_id when the override
    # was exercised. Migration 031 added the columns; pre-031 builds
    # would error here.
    row = await conn.fetchrow(
        """
        UPDATE job_card_v2
           SET status         = 'completed',
               end_time       = NOW(),
               total_time_min = CASE
                                  WHEN $2::bool AND start_time IS NOT NULL
                                  THEN EXTRACT(EPOCH FROM (NOW() - start_time)) / 60.0
                                  ELSE total_time_min
                                END,
               force_closed           = CASE WHEN $3::bool THEN TRUE ELSE force_closed END,
               force_close_request_id = CASE WHEN $3::bool THEN $4 ELSE force_close_request_id END,
               force_close_by         = CASE WHEN $3::bool THEN $5 ELSE force_close_by END,
               force_close_at         = CASE WHEN $3::bool THEN NOW() ELSE force_close_at END
         WHERE job_card_id = $1
        RETURNING status, end_time, total_time_min, force_closed
        """,
        job_card_id, use_fallback,
        override_valid,
        request_id if override_valid else None,
        completed_by if override_valid else None,
    )
    return {
        "completed":      True,
        "job_card_id":    job_card_id,
        "status":         row["status"],
        "end_time":       row["end_time"].isoformat() if row["end_time"] else None,
        "total_time_min": float(row["total_time_min"]) if row["total_time_min"] is not None else None,
    }


async def force_unlock(conn, *, job_card_id: int,
                        authority: str, reason: str) -> dict:
    """Admin override: flip a locked JC to 'unlocked' regardless of
    upstream-handoff state. Stamps `force_unlocked=true` + audit fields
    so the override is visible on the detail screen.

    Refused on a JC that's already unlocked or past 'unlocked' status —
    there's nothing to override.
    """
    if not authority or not authority.strip():
        return {"error": "missing_authority", "message": "authority is required"}
    if not reason or not reason.strip():
        return {"error": "missing_reason",    "message": "reason is required"}

    jc = await conn.fetchrow(
        "SELECT status, is_locked FROM job_card_v2 WHERE job_card_id=$1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if not jc["is_locked"]:
        return {"error": "not_locked",
                "message": "JC is already unlocked; nothing to force"}

    row = await conn.fetchrow(
        """
        UPDATE job_card_v2
           SET is_locked          = FALSE,
               status             = 'unlocked',
               locked_reason      = NULL,
               force_unlocked     = TRUE,
               force_unlock_by    = $2,
               force_unlock_reason = $3,
               force_unlock_at    = NOW()
         WHERE job_card_id = $1
        RETURNING *
        """,
        job_card_id, authority.strip(), reason.strip(),
    )
    return {"force_unlocked": True, "job_card": _serialize(row)}


# Columns the patch endpoint is allowed to write. Anything outside this
# set is dropped on the floor — keeps callers from quietly mutating
# lineage / lifecycle / audit columns.
_PATCH_ALLOWED_COLUMNS = frozenset({
    'fg_sku_name', 'customer_name', 'batch_number',
    'planned_qty_kg', 'planned_qty_units', 'uom',
    'assigned_to_team_leader', 'team_members',
    'floor', 'machine_id',
})

# R1 gate: header edits are allowed up to and including 'material_received'.
# Once the JC reaches 'in_progress' (or any terminal status) the only way to
# change state is operational entry - output, accounting, QC. Mirrors
# jc_editor.EDITABLE_STATUSES on the v1 path; intentionally not imported so
# the two paths can diverge later if their rules drift.
PATCHABLE_STATUSES = frozenset({"locked", "unlocked", "assigned", "material_received"})


async def patch_job_card(conn, *, job_card_id: int,
                          fields: dict, updated_by: str | None = None) -> dict:
    """Partial update of header fields. Fields outside the allow-list
    (status, plan lineage, audit columns, chain pointers) are silently
    ignored — use the dedicated lifecycle endpoints for those.

    R1 status gate: PATCH is blocked once the JC reaches 'in_progress'
    or a terminal state. Lifecycle endpoints handle those transitions.

    Returns the updated row, or {"error": "no_change"} when nothing in
    the payload was an allowed column.
    """
    # R1 status gate - pre-fetch so we can return a specific error rather
    # than letting an UPDATE with an extra WHERE clause silently no-op.
    jc = await conn.fetchrow(
        "SELECT status FROM job_card_v2 "
        "WHERE  job_card_id=$1 AND deleted_at IS NULL",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] not in PATCHABLE_STATUSES:
        return {
            "error": "invalid_status",
            "status": jc["status"],
            "message": (
                f"Header edits blocked in status '{jc['status']}'. "
                "Use the lifecycle endpoints (/start, /complete, /close, "
                "/stop) for state transitions."
            ),
        }

    sets: list[str] = []
    params: list = []
    idx = 1
    for k, v in (fields or {}).items():
        if k not in _PATCH_ALLOWED_COLUMNS:
            continue
        sets.append(f'"{k}" = ${idx}')
        params.append(v); idx += 1
    if not sets:
        return {"error": "no_change",
                "message": "No editable fields supplied"}
    # Stamp the audit trail too.
    sets.append(f'"updated_by" = ${idx}'); params.append(updated_by); idx += 1
    sets.append('"updated_at" = NOW()')
    params.append(job_card_id)

    row = await conn.fetchrow(
        f'UPDATE job_card_v2 SET {", ".join(sets)} '
        f'WHERE job_card_id = ${idx} AND deleted_at IS NULL RETURNING *',
        *params,
    )
    if not row:
        return {"error": "job_card_not_found"}
    return {"updated": True, "job_card": _serialize(row)}


async def cancel_job_card(conn, *, job_card_id: int,
                           reason: str, deleted_by: str | None = None) -> dict:
    """Soft-cancel a JC. Only permitted on JCs that haven't started yet
    (locked / unlocked / assigned / material_received); a cancel after
    'in_progress' would orphan material that's already flowing.

    Side effects:
        - cancelled_snapshot stamped with the full pre-cancel detail
          payload (migration 043) so a later read of the cancelled JC
          can reconstruct exactly what existed at cancel time even if
          linked tables are mutated afterwards.
        - status = 'cancelled', deleted_at = NOW()
        - cancellation_reason recorded
        - the downstream JC (if it was waiting on this stage's handoff)
          is unaffected; the operator can still complete normally by
          ignoring the dispatch button on the cancelled card.
    """
    if not reason or not reason.strip():
        return {"error": "missing_reason", "message": "reason is required"}

    jc = await conn.fetchrow(
        """
        SELECT status FROM job_card_v2
        WHERE  job_card_id = $1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] not in ('locked', 'unlocked', 'assigned', 'material_received'):
        return {"error": "invalid_status",
                "message": f"Cannot cancel a JC in '{jc['status']}' — use close instead"}

    # Build the cancellation snapshot before flipping status. We compose
    # it from the canonical detail builder so the JSONB blob matches what
    # GET /job-cards-v2/{id} returns — saves a future reader from having
    # to relearn the shape, and stays in lockstep when the detail surface
    # gains new sections. Any failure here is fatal (the transaction will
    # roll back) — silent snapshot drop would defeat the purpose.
    import json
    snapshot_payload = await get_job_card(conn, job_card_id)
    snapshot_json = json.dumps(
        snapshot_payload,
        default=str,         # stringify Decimal / datetime cleanly
        ensure_ascii=False,
    )

    row = await conn.fetchrow(
        """
        UPDATE job_card_v2
           SET status              = 'cancelled',
               deleted_at          = NOW(),
               deleted_by          = $2,
               cancellation_reason = $3,
               cancelled_snapshot  = $4::jsonb
         WHERE job_card_id = $1
        RETURNING *
        """,
        job_card_id, deleted_by, reason.strip(), snapshot_json,
    )
    return {"cancelled": True, "job_card": _serialize(row)}


# ---------------------------------------------------------------------------
# Stop Process (R1)
# ---------------------------------------------------------------------------

async def stop_job_card(conn, *, job_card_id: int,
                        reason: str, stopped_by: str | None = None,
                        request_id: int | None = None) -> dict:
    """R1 Stop Process: mid-run cancellation for JCs that are receiving
    material or already running. Distinct from cancel_job_card (which only
    handles the pre-start range) - this is the operator-facing 'stop
    everything' button for material_received / in_progress JCs.

    Side effects in one txn:
      - Any OPEN R13 batch for this JC is set to 'cancelled' first.
        Batch-table partial UNIQUE index uq_jcbatch_one_open guarantees
        at most one open batch, so a single UPDATE handles it.
      - job_card_v2.status -> 'cancelled', deleted_at = NOW(),
        cancellation_reason carries the audit prefix
        '[STOP_PROCESS][req:<id>] ' when request_id is supplied,
        '[STOP_PROCESS] ' otherwise. The bracketed prefix is grep-safe
        so future audits can correlate the JC cancel with the R8
        amendment that authorised it. The dedicated FK column lands
        with B11.

    Approval composition: per R8 matrix row 8 (floor_manager maker,
    admin / production_manager checker). Approval enforcement still
    lives upstream - this service just performs the state transition.
    """
    if not reason or not reason.strip():
        return {"error": "missing_reason", "message": "reason is required"}

    # FOR UPDATE serialises concurrent /stop calls on the same JC so two
    # operators racing the button can't both pass the status check and
    # overwrite each other's cancellation_reason. asyncpg passes the
    # row-lock through the wrapping conn.transaction(); the lock releases
    # when the txn commits/rolls back at the router layer.
    jc = await conn.fetchrow(
        """
        SELECT status FROM job_card_v2
        WHERE  job_card_id = $1 AND deleted_at IS NULL
        FOR    UPDATE
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] not in ('material_received', 'in_progress'):
        return {"error": "invalid_status",
                "message": (
                    f"Cannot stop a JC in '{jc['status']}'. Stop-process "
                    "applies only to 'material_received' or 'in_progress'. "
                    "Use cancel for pre-start; close for completed."
                )}

    # Close any open shift segment so the partial-unique index
    # uq_jcsl_v2_one_open doesn't leave a dangling open row that future
    # /shifts/start calls (or analytics) trip over. Mirrors the manual
    # stop_shift path - end_at = NOW(), paused_minutes left at 0 since
    # the operator didn't intend a normal stop.
    await conn.execute(
        """
        UPDATE job_card_shift_log_v2
           SET end_at = NOW(),
               notes  = COALESCE(notes || E'\n', '') ||
                        'Closed by stop-process: ' || $2
         WHERE job_card_id = $1
           AND end_at IS NULL
        """,
        job_card_id, reason.strip(),
    )

    # Cancel any open batch next (single-open invariant from migration 029,
    # table renamed in 036; constraint preserved for Stage 1).
    await conn.execute(
        """
        UPDATE job_card_batch_v2
           SET status    = 'cancelled',
               ended_at  = COALESCE(ended_at, NOW()),
               closed_at = NOW(),
               closed_by = $2,
               notes     = COALESCE(notes || E'\n', '') ||
                           'Cancelled by stop-process: ' || $3
         WHERE job_card_id = $1
           AND status      = 'open'
        """,
        job_card_id, stopped_by, reason.strip(),
    )

    prefix = (
        f"[STOP_PROCESS][req:{request_id}] "
        if request_id is not None
        else "[STOP_PROCESS] "
    )
    row = await conn.fetchrow(
        """
        UPDATE job_card_v2
           SET status              = 'cancelled',
               deleted_at          = NOW(),
               deleted_by          = $2,
               cancellation_reason = $4 || $3
         WHERE job_card_id = $1
        RETURNING *
        """,
        job_card_id, stopped_by, reason.strip(), prefix,
    )
    return {"stopped": True, "job_card": _serialize(row)}


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

# Roles that must sign off before a JC may be closed.
#
# Per the operations team's 2026-05 decision, only the production head's
# sign-off gates close. Floor in-charge and QC inspector sign-offs were
# previously required but were removed — those roles still exist (and
# can still sign for audit), but their signature is no longer mandatory
# to close the JC.
REQUIRED_SIGN_OFFS = ('production_head',)


async def close_job_card(conn, *, job_card_id: int,
                         allow_partial: bool = False) -> dict:
    """Transition a v2 JC to status='closed'. Refuses when:
        - any required sign-off role is missing
        - any open shift segment exists
        - the JC is already closed or cancelled

    `allow_partial` skips the sign-off check (admin override). Use sparingly.

    After a successful close, the caller should run maybe_close_plan_from_jcs
    to roll up plan status. (Endpoint wrapper handles that.)
    """
    jc = await conn.fetchrow(
        """
        SELECT job_card_id, status, plan_id
        FROM   job_card_v2
        WHERE  job_card_id=$1 AND deleted_at IS NULL
        """,
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] in ('closed', 'cancelled'):
        return {"error": "terminal_state", "current_status": jc["status"]}

    # Block close while a shift is still open — closing while clock is
    # running would make total_time_min permanently wrong.
    open_segments = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_shift_log_v2 WHERE job_card_id=$1 AND end_at IS NULL",
        job_card_id,
    )
    if open_segments:
        return {"error": "open_shift",
                "message": "Stop the open shift segment before closing"}

    if not allow_partial:
        existing_roles = {
            r["role"] for r in await conn.fetch(
                "SELECT role FROM job_card_sign_off_v2 WHERE job_card_id=$1",
                job_card_id,
            )
        }
        missing = [r for r in REQUIRED_SIGN_OFFS if r not in existing_roles]
        if missing:
            return {"error": "missing_sign_offs", "missing": missing}

    closed = await conn.fetchrow(
        """
        UPDATE job_card_v2
           SET status   = 'closed',
               end_time = COALESCE(end_time, NOW())
         WHERE job_card_id=$1
        RETURNING *
        """,
        job_card_id,
    )
    return {"closed": True, "job_card": _serialize(closed), "plan_id": jc["plan_id"]}


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

async def get_job_card(conn, job_card_id: int) -> dict | None:
    """Full JC detail. Returns the v2 flat shape PLUS the v1 sectioned
    shape (`section_1_product`, `section_3_team`, `section_5_output`,
    `section_6_sign_offs`, `annexure_*`) so existing v1-shaped clients
    (Android detail screen, legacy desktop drawer) keep rendering against
    v2 data without code changes.

    Annexure tables (metal detection, weight checks, environment, loss
    reconciliation, remarks, byproducts, store_allocations) don't exist
    in v2 yet — they're returned as empty arrays. UI code that iterates
    those lists tolerates the empty case.
    """
    header = await conn.fetchrow(
        "SELECT * FROM job_card_v2 WHERE job_card_id=$1",
        job_card_id,
    )
    if not header:
        return None
    h = _serialize(header)

    shifts   = await list_shifts(conn, job_card_id)
    outputs  = await conn.fetch("SELECT * FROM job_card_output_v2  WHERE job_card_id=$1 ORDER BY recorded_at", job_card_id)
    rm       = await conn.fetch("SELECT * FROM job_card_rm_indent_v2 WHERE job_card_id=$1 ORDER BY rm_indent_id", job_card_id)
    pm       = await conn.fetch("SELECT * FROM job_card_pm_indent_v2 WHERE job_card_id=$1 ORDER BY pm_indent_id", job_card_id)
    signoffs = await conn.fetch("SELECT * FROM job_card_sign_off_v2 WHERE job_card_id=$1 ORDER BY signed_at", job_card_id)

    # ─── Enrich product info via plan_line → SO + BOM joins ───────────────
    # The JC carries plan_line_id; production_plan_line_v2.linked_so_fulfillment_ids
    # is an array of so_fulfillment_v2 PKs. We take the first fulfillment
    # (a plan line can satisfy multiple SO lines, but for the overview we
    # surface the leading one and ALL of them as a list).
    so_numbers: list[str] = []
    so_dates:   list[str] = []
    so_lines:   list[dict] = []
    bom_item_group: str | None = None
    bom_pack_size:  float | None = None
    bom_version:    int | None = None
    sales_order_ref: str | None = None

    if h.get("plan_line_id"):
        # SO chain: plan_line → so_fulfillment_v2 → so_line → so_header
        so_rows = await conn.fetch(
            """
            SELECT h.so_id, h.so_number, h.so_date,
                   l.so_line_id, l.line_number, l.uom, l.rate_inr,
                   l.quantity AS so_qty, l.quantity_units AS so_qty_units
            FROM   production_plan_line_v2 pl
            JOIN   so_fulfillment_v2 f
                   ON f.so_fulfillment_id = ANY(pl.linked_so_fulfillment_ids)
            JOIN   so_line   l ON l.so_line_id = f.so_line_id
            JOIN   so_header h ON h.so_id     = l.so_id
            WHERE  pl.plan_line_id = $1
            ORDER  BY h.so_date NULLS LAST, h.so_number
            """,
            h["plan_line_id"],
        )
        for r in so_rows:
            d = _serialize(r)
            so_lines.append(d)
            if d.get("so_number"):
                so_numbers.append(d["so_number"])
            if d.get("so_date"):
                so_dates.append(d["so_date"])
        # Pretty-print "SO-2026-001, SO-2026-002" when multiple are linked.
        sales_order_ref = ", ".join(dict.fromkeys(so_numbers)) if so_numbers else None

    # BOM enrichment: item_group + pack_size_kg + version (catalog metadata
    # the overview shows under "Business Unit" / "Pack Size").
    if h.get("bom_id"):
        bom_row = await conn.fetchrow(
            """
            SELECT item_group, pack_size_kg, version
            FROM   bom_header
            WHERE  bom_id = $1
            """,
            h["bom_id"],
        )
        if bom_row:
            bom_item_group = bom_row["item_group"]
            bom_pack_size  = float(bom_row["pack_size_kg"]) if bom_row["pack_size_kg"] is not None else None
            bom_version    = bom_row["version"]

    # BOM catalogue — the full list of articles (RM + PM) attached to
    # this JC's product, regardless of which stage they're issued on.
    # Surfaced so the Output tab's rejection / balance / extra-giveaway
    # dropdowns can show every BOM article on every stage, not just the
    # ones materialised into RM/PM indent rows for this specific JC.
    bom_line_rows = []
    if h.get("bom_id"):
        bom_line_rows = await conn.fetch(
            """
            SELECT bom_line_id, line_number, material_sku_name, item_type,
                   uom, quantity_per_unit, loss_pct, godown
            FROM   bom_line
            WHERE  bom_id = $1
            ORDER  BY item_type, line_number
            """,
            h["bom_id"],
        )

    # Per-stage Material Consumption rows. The Output tab pre-fills its
    # consumption inputs from this list — one row per (JC, BOM article)
    # that the operator has previously recorded. RM+PM both materialise
    # at stage 1, but the LAST stage (packaging) commonly records
    # consumption against BOTH kinds because the packaging line uses
    # PM plus any trailing RM (flavourings, etc).
    # Stage 2: batch_id is added by migration 038.  When the server is
    # restarted on Stage 2 code but the migration hasn't landed yet,
    # asyncpg raises UndefinedColumnError on the SELECT.  Fall back to
    # the pre-Stage-2 column list with batch_id surfaced as NULL so the
    # JC detail page stays loadable until psql is run.
    try:
        consumption_rows = await conn.fetch(
            """
            SELECT consumption_id, batch_id, bom_line_id, material_sku_name,
                   input_kind, uom, issued_qty, actual_consumed_qty, return_qty,
                   variance, remarks
            FROM   job_card_material_consumption_v2
            WHERE  job_card_id = $1
            ORDER  BY input_kind, material_sku_name
            """,
            job_card_id,
        )
    except UndefinedColumnError:
        _warn_batch_id_missing_once("job_card_material_consumption_v2")
        consumption_rows = await conn.fetch(
            """
            SELECT consumption_id, NULL::BIGINT AS batch_id, bom_line_id,
                   material_sku_name, input_kind, uom, issued_qty,
                   actual_consumed_qty, return_qty, variance, remarks
            FROM   job_card_material_consumption_v2
            WHERE  job_card_id = $1
            ORDER  BY input_kind, material_sku_name
            """,
            job_card_id,
        )

    # Byproducts in the legacy `{byproduct_id, category, qty_kg, remarks}`
    # shape the Android Output form deserialises (ByproductLine.java).
    # Column `quantity` is aliased to `qty_kg` so the JSON keys round-trip
    # without a client-side mapping layer.
    #
    # Migration 034 added material_name + bom_line_id so off-grade /
    # rejection rows can persist their article attribution. These are
    # NULL for control_sample / pm_* / dust / etc.
    try:
        byproduct_rows = await conn.fetch(
            """
            SELECT byproduct_id, batch_id, category, quantity AS qty_kg, uom,
                   remarks, material_name, bom_line_id
            FROM   job_card_byproducts_v2
            WHERE  job_card_id = $1
            ORDER  BY category, COALESCE(material_name, '')
            """,
            job_card_id,
        )
    except UndefinedColumnError:
        _warn_batch_id_missing_once("job_card_byproducts_v2")
        byproduct_rows = await conn.fetch(
            """
            SELECT byproduct_id, NULL::BIGINT AS batch_id, category,
                   quantity AS qty_kg, uom, remarks, material_name, bom_line_id
            FROM   job_card_byproducts_v2
            WHERE  job_card_id = $1
            ORDER  BY category, COALESCE(material_name, '')
            """,
            job_card_id,
        )
    # Balance materials (migration 027). Returned per-row so the Output
    # form can pre-fill the qty input next to each BOM article.
    try:
        balance_material_rows = await conn.fetch(
            """
            SELECT balance_id, batch_id, bom_line_id, material_id, material_name,
                   balance_type, qty_kg, remarks
            FROM   job_card_balance_material_v2
            WHERE  job_card_id = $1
            ORDER  BY balance_type, material_name
            """,
            job_card_id,
        )
    except UndefinedColumnError:
        _warn_batch_id_missing_once("job_card_balance_material_v2")
        balance_material_rows = await conn.fetch(
            """
            SELECT balance_id, NULL::BIGINT AS batch_id, bom_line_id,
                   material_id, material_name, balance_type, qty_kg, remarks
            FROM   job_card_balance_material_v2
            WHERE  job_card_id = $1
            ORDER  BY balance_type, material_name
            """,
            job_card_id,
        )
    # QC summary (migration 027). At most one row per JC.
    qc_row = await conn.fetchrow(
        """
        SELECT qc_id, result, findings, corrective_action, inspector_user,
               inspection_date
        FROM   job_card_qc_v2
        WHERE  job_card_id = $1
        """,
        job_card_id,
    )

    # ─── FG per-unit kg from all_sku (R2 canonical source) ───────────────
    # all_sku.uom is the per-unit kg multiplier (NUMERIC(15,3)) per
    # schema.sql:54 — single source of truth. bom_header.pack_size_kg is a
    # downstream snapshot kept as fallback for FG SKUs not yet in the master
    # (or master rows where uom is NULL).
    sku_uom: float | None = None
    if h.get("fg_sku_name"):
        sku_row = await conn.fetchrow(
            "SELECT uom FROM all_sku WHERE particulars = $1 LIMIT 1",
            h["fg_sku_name"],
        )
        if sku_row and sku_row["uom"] is not None:
            sku_uom = float(sku_row["uom"])
    net_wt_per_unit_kg: float | None = (
        sku_uom if (sku_uom is not None and sku_uom > 0) else bom_pack_size
    )
    # expected_units: prefer the planner's stored planned_qty_units (R2
    # primary input); otherwise derive from planned_qty_kg ÷ net_wt_per_unit_kg.
    expected_units: int | None = None
    if h.get("planned_qty_units") is not None:
        expected_units = int(h["planned_qty_units"])
    elif (
        net_wt_per_unit_kg is not None
        and net_wt_per_unit_kg > 0
        and h.get("planned_qty_kg") is not None
    ):
        try:
            expected_units = int(round(float(h["planned_qty_kg"]) / net_wt_per_unit_kg))
        except (TypeError, ValueError):
            expected_units = None

    # ─── v1 compat: sectioned payload derived from v2 data ───────────────
    section_1_product = {
        "customer_name":  h.get("customer_name"),
        "fg_sku_name":    h.get("fg_sku_name"),
        "batch_number":   h.get("batch_number"),
        "batch_size_kg":  h.get("planned_qty_kg"),
        "quantity_units": int(h["planned_qty_units"]) if h.get("planned_qty_units") is not None else None,
        "factory":        h.get("factory"),
        "floor":          h.get("floor"),
        # SO + BOM enrichment (new — see joins above)
        "so_number":       so_numbers[0] if so_numbers else None,
        "so_numbers":      so_numbers,                   # full list when multiple SOs feed this plan line
        "so_date":         so_dates[0] if so_dates else None,
        "sales_order_ref": sales_order_ref,              # mirror for v1 callers that read this key
        "business_unit":   bom_item_group,               # bom_header.item_group
        "item_group":      bom_item_group,
        "pack_size_kg":    bom_pack_size,
        "bom_version":     bom_version,
        # Stage + lineage context handy for the overview header
        "step_number":     h.get("step_number"),
        "process_name":    h.get("process_name"),
        "stage":           h.get("stage"),
        "input_kind":      h.get("input_kind"),
        "output_kind":     h.get("output_kind"),
        "plan_id":         h.get("plan_id"),
        "plan_line_id":    h.get("plan_line_id"),
        "uom":             h.get("uom"),
        # Aliases the v1 detail screen looks for; not in the v2 schema today.
        # Kept as null so the UI shows "--" instead of erroring out.
        "article_code":         None,
        # net_wt_per_unit_kg + expected_units now sourced from all_sku.uom
        # (R2 framework). See block above this dict where the lookup runs.
        "net_wt_per_unit_kg":   net_wt_per_unit_kg,
        "expected_units":       expected_units,
        "mrp":                  None,
        "ean_code":             None,
        "best_before_date":     None,
        "shelf_life_days":      None,
    }
    section_3_team = {
        "team_leader":    h.get("assigned_to_team_leader"),
        "team_members":   h.get("team_members") or [],
        "batch_number":   h.get("batch_number"),
        "start_time":     h.get("start_time"),
        "end_time":       h.get("end_time"),
        "total_time_min": h.get("total_time_min"),
    }
    # Latest output row collapses to the legacy single-output section.
    last_output = _serialize(outputs[-1]) if outputs else None
    section_5_output = {
        "output_id":       last_output.get("output_id")  if last_output else None,
        "fg_actual_kg":    last_output.get("output_qty_kg") if last_output else None,
        "fg_actual_units": int(last_output["output_qty_units"]) if (last_output and last_output.get("output_qty_units") is not None) else None,
        "rm_consumed_kg":  last_output.get("rm_consumed_kg") if last_output else None,
        "process_loss_kg": last_output.get("process_loss_kg") if last_output else None,
        "yield_pct":       last_output.get("yield_pct")  if last_output else None,
        "created_at":      last_output.get("recorded_at") if last_output else None,
    }
    # Sign-offs in the legacy list shape (one entry per role).
    section_6_sign_offs = [
        {"role": r["role"], "signed_by": r["signed_by"], "signed_at": _serialize(r)["signed_at"]}
        for r in signoffs
    ]
    # RM / PM indent in the legacy "IndentLine" shape — same field names.
    section_2a_rm_indent = [_serialize(r) for r in rm]
    section_2b_pm_indent = [_serialize(r) for r in pm]

    return {
        # v2 native shape
        **h,
        "shift_log":  shifts,
        "outputs":    [_serialize(r) for r in outputs],
        "rm_indents":        section_2a_rm_indent,
        "pm_indents":        section_2b_pm_indent,
        "bom_lines":         [_serialize(r) for r in bom_line_rows],
        "consumption_lines": [_serialize(r) for r in consumption_rows],
        "sign_offs":         [_serialize(r) for r in signoffs],

        # SO linkage at top level — full list so callers can render every SO
        # this JC's plan line satisfies, not just the first one.
        "so_lines":          so_lines,
        "so_numbers":        so_numbers,
        "primary_so_number": so_numbers[0] if so_numbers else None,

        # v1 compat shape (Android detail, legacy desktop drawer)
        "section_1_product":    section_1_product,
        "section_2a_rm_indent": section_2a_rm_indent,
        "section_2b_pm_indent": section_2b_pm_indent,
        "section_3_team":       section_3_team,
        "section_5_output":     section_5_output,
        "section_6_sign_offs":  section_6_sign_offs,
        # Annexure v2 tables (migration 020) — populated when present.
        # Empty arrays for v2 JCs that haven't recorded annexure rows yet.
        "annexure_a_b_metal_detection":   await _annexure_rows(conn, 'metal_detection', job_card_id),
        "annexure_b_weight_checks":       await _annexure_rows(conn, 'weight_check', job_card_id),
        "annexure_c_environment":         await _annexure_rows(conn, 'environment', job_card_id),
        "annexure_d_loss_reconciliation": await _annexure_rows(conn, 'loss_reconciliation', job_card_id),
        "annexure_e_remarks":             await _annexure_rows(conn, 'remarks', job_card_id),
        "byproducts":                     [_serialize(r) for r in byproduct_rows],
        "balance_materials":              [_serialize(r) for r in balance_material_rows],
        # Additives (035) — data-keeping consumption bucket. Lazy import
        # to avoid a circular dependency between job_card_v2 and
        # jc_additives_v2 (the latter pulls helpers from job_card_v2 in
        # future iterations).
        "additives": await _list_additives_local(conn, job_card_id),
        "qc":                             _serialize(qc_row) if qc_row else None,
        "store_allocations":              [],          # see /allocations endpoint (TODO)
        # total_stages for the chain progress bar.
        "total_stages": await conn.fetchval(
            "SELECT COUNT(*) FROM job_card_v2 WHERE plan_line_id=$1 AND deleted_at IS NULL",
            h.get("plan_line_id"),
        ) if h.get("plan_line_id") else None,
    }
