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

from asyncpg.exceptions import CheckViolationError, UndefinedColumnError

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
#
# Caveat (audit DERIVE-STAGE-STRING): this substring test is deliberately
# loose, so it can MIS-CLASSIFY edge names — a real packing operation whose
# name lacks the token (e.g. "Pouch Filling" → "pouch_filling") reads as
# non-packing and the EGA form is refused for it. The canonical classifier is
# processCatalog.classifySteps (client) / master_ingest.classify_route_steps
# (server). A future hardening should gate EGA on an explicit stage-catalogue
# lookup rather than this string match — see the Create-Job-Card backend design.
# Behaviour is intentionally LEFT AS-IS here to avoid shifting the EGA gate
# across the (locked) SFG vertical without that catalogue in place.
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
    `input_kind` is the consumption kind: 'RM' / 'PM' / 'SFG' / 'WIP'. RM/PM
    come from the BOM catalog; SFG/WIP rows (Slice 4, gate G1 Option B) are an
    opening input the FG stage consumes — they may carry a nullable bom_line_id
    (the sfg bom_line) and a per-entry `source_dispatch_id` linking the
    consumption back to the prev-stage dispatch that produced the SFG.

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
    if input_kind not in ('RM', 'PM', 'SFG', 'WIP'):
        return 0
    written = 0
    for e in entries:
        sku = e.get("material_sku_name")
        qty = e.get("consumed_qty")
        if not sku or qty is None:
            continue   # skip malformed entries silently — backend validated
                       # bom_line_id at the router layer already
        # Per-entry input_kind override (Slice 4): a mixed bucket (e.g. the
        # /outputs rm_consumed list now carries SFG/WIP opening-input rows) tags
        # each row with its own kind; fall back to the function-level default.
        row_kind = (e.get("input_kind") or input_kind or "").upper()
        if row_kind not in ('RM', 'PM', 'SFG', 'WIP'):
            continue   # skip a row with an unknown kind rather than abort the txn
        bom_line_id = e.get("bom_line_id")
        uom         = e.get("uom") or "KGS"
        remarks     = e.get("remarks")
        src_dispatch = e.get("source_dispatch_id")
        # Snapshot the prior consumed qty so a genuine edit is recorded in the JC
        # edit log (drives the FE red marker). None → new row (first entry, not an
        # edit). Keyed on the same (JC, batch, material) as the upsert.
        old_actual = await conn.fetchval(
            "SELECT actual_consumed_qty FROM job_card_material_consumption_v2 "
            "WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = COALESCE($2::bigint, 0) "
            "AND material_sku_name = $3",
            job_card_id, batch_id, sku,
        )
        async def _upsert(_sku=sku, _kind=row_kind, _uom=uom,
                          _qty=qty, _bom_id=bom_line_id, _rem=remarks,
                          _rec_by=recorded_by, _batch=batch_id,
                          _src_dispatch=src_dispatch):
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
                    remarks, recorded_by, source_dispatch_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, 0, $9, $10, $11)
                ON CONFLICT (job_card_id, COALESCE(batch_id, 0),
                             material_sku_name)
                DO UPDATE SET
                    bom_line_id         = EXCLUDED.bom_line_id,
                    -- Promote batch_id so a save against batch X always
                    -- leaves the row tagged with X. Without this, the
                    -- ON CONFLICT key's COALESCE(batch_id, 0) can match a
                    -- legacy batch_id=NULL row (or one mistakenly written
                    -- by an older record_output that ignored batch_id),
                    -- and the DO UPDATE would otherwise preserve the
                    -- stale NULL — stranding the row under the legacy
                    -- bucket and blanking the operator's form on the
                    -- next read because matchesBatch couldn't find it.
                    batch_id            = EXCLUDED.batch_id,
                    input_kind          = EXCLUDED.input_kind,
                    uom                 = EXCLUDED.uom,
                    actual_consumed_qty = EXCLUDED.actual_consumed_qty,
                    remarks             = EXCLUDED.remarks,
                    recorded_by         = EXCLUDED.recorded_by,
                    -- Preserve a previously-set dispatch link when a routine
                    -- re-save omits it (COALESCE keeps the existing value).
                    source_dispatch_id  = COALESCE(EXCLUDED.source_dispatch_id,
                                                   job_card_material_consumption_v2.source_dispatch_id)
                RETURNING consumption_id
                """,
                new_short_time_id(), job_card_id, _bom_id, _batch,
                _sku, _kind, _uom, float(_qty), _rem, _rec_by, _src_dispatch,
            )
        await insert_with_pk_retry(conn, _upsert)
        written += 1
        # Audit a real change to an existing material's consumed qty. Compare at the
        # column's 3-dp precision (NUMERIC) via rounded floats so a re-save of the
        # same value isn't logged as a change. field_name =
        # 'consumption:<batch>:<material>.actual_consumed_qty' (or no-batch form).
        if recorded_by and old_actual is not None:
            from app.modules.production.services.amendment_service import log_jc_field_changes
            prefix = f"consumption:{batch_id}:{sku}." if batch_id else f"consumption:{sku}."
            await log_jc_field_changes(
                conn, job_card_id=job_card_id, record_type="job_card",
                field_prefix=prefix, changed_by=recorded_by, reason="consumption edit",
                before={"actual_consumed_qty": round(float(old_actual), 3)},
                after={"actual_consumed_qty": round(float(qty), 3)},
            )
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


async def _resolve_sfg_seam_code(conn, bom_id: int | None) -> str | None:
    """Return the SFG#### a 2-stage routed article produces — the Create-WIP
    step's ``output_code`` in bom_process_route — or None for a single-stage /
    unrouted article (Slice 3).

    Used to stamp the chain seam at JC creation so that, for a 2-stage plan,
    ``JC1.output_code == JC2.input_code == SFG####``. There is at most one
    SFG-producing step per article (the routing plug is 1-or-2 steps), so the
    ORDER BY step_number / LIMIT 1 is belt-and-braces.
    """
    if not bom_id:
        return None
    return await conn.fetchval(
        """
        SELECT output_code
          FROM bom_process_route
         WHERE bom_id = $1
           AND output_kind = 'SFG'
           AND output_code IS NOT NULL
         ORDER BY step_number
         LIMIT 1
        """,
        bom_id,
    )


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

        # Slice 3: the SFG#### this 2-stage article produces (Create-WIP step's
        # output_code in bom_process_route), or None for single-stage / unrouted.
        sfg_code = await _resolve_sfg_seam_code(conn, ln["bom_id"])
        # A routed SFG article's route is exactly 2 steps (Create WIP -> Final FG),
        # so the position-based seam below is only trustworthy when the plan kept
        # that 2-step shape. If an admin added/removed steps the plan diverged —
        # stamping by position would put the code on the wrong step, so skip the
        # seam (codes left NULL) and warn rather than mis-wire it.
        if sfg_code and step_count != 2:
            logger.warning(
                "plan %s line %s: routed 2-stage article (SFG %s) but plan has "
                "%d step(s) — SFG seam not materialised (expected 2)",
                plan_id, plan_line_id, sfg_code, step_count,
            )

        for idx, step in enumerate(step_rows):
            is_first = idx == 0
            is_last  = idx == step_count - 1

            # Material-flow context per spec (see module docstring).
            input_kind  = 'RM'  if is_first else 'SFG'
            output_kind = 'FG'  if is_last  else 'WIP'

            # Slice 3 — wire the SFG seam on the Create-WIP -> Final-FG handoff
            # (the last seam of a >=2 step chain): the producer (second-to-last
            # step) emits SFG####; the consumer (last step) opens with it. Only
            # fires for a routed 2-stage article (sfg_code present); single-stage
            # and unrouted chains keep the RM->WIP->FG defaults unchanged.
            input_code:  str | None = None
            output_code: str | None = None
            if sfg_code and step_count == 2:
                if idx == 0:                     # SFG producer (Create WIP)
                    output_kind = 'SFG'
                    output_code = sfg_code
                else:                            # SFG consumer (Final FG; input_kind='SFG')
                    input_code = sfg_code

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
                _input_code=input_code, _output_code=output_code,
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
                        prev_job_card_id,
                        input_code, output_code
                    ) VALUES (
                        $1, $2,
                        $3, $4, $5, $6,
                        $7, $8, $9,
                        $10, $11, $12,
                        $13, $14, $15,
                        $16, $17,
                        $18, $19, $20,
                        $21, $22, $23,
                        $24,
                        $25, $26
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
                    _input_code, _output_code,
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
# Per-article (wizard) job-card creation — the Plan-List "Create Job Card"
# flow. Chained-per-process + mutable plan steps, per the signed-off design
# in docs/SO_Planning_Convergence_Remediation.md §4.
# ---------------------------------------------------------------------------

async def create_job_cards_for_line(
    conn,
    plan_line_id: int,
    *,
    qty_kg,
    qty_units=None,
    wip_steps: list[dict],
    pkg_floor: str,
    pkg_process: str = "Packaging",
) -> dict:
    """Create one chained job_card_v2 per WIP process → a terminating Packaging
    JC for a SINGLE plan line, and dispatch each to its floor.

    Driven by the wizard's per-article inputs (NOT the plan's snapshot steps):
      * wip_steps: [{process, floor, sfg_output}] in operator order;
      * pkg_floor: the packaging (Final FG) floor.
      * pkg_process: the terminal step's process name — defaults to
        "Packaging" but carries the operator's (possibly merged, e.g.
        "Sorting + Packing") label from the unified process list. The stage
        stays 'packaging'/FG regardless of the name.

    Behaviour mirrors create_job_cards_from_plan's chain machinery so the rest
    of the system (lock/handoff/indents/PDF/annexure) keeps working:
      * stage-1 `unlocked` (ready on its floor), the rest `locked` +
        `awaiting_previous_stage`, released downstream by dispatch_to_next;
      * RM + PM materialised on stage 1 ONLY, from the LINE's bom_id (the
        wizard supplies no material data);
      * input_kind first=RM / else SFG; output_kind last=FG / else WIP, with a
        producer promoted to output_kind='SFG'+output_code when it declares an
        sfg_output. The consumer opens with the previous producer's code
        (input_code). A producer with no sfg_output stays plain WIP (NULL code)
        — the legitimate multi-step WIP case the dispatch backstop allows.

    Mutable steps: the line's production_plan_step_v2 rows are REPLACED with the
    wizard's (satisfies job_card_v2.plan_step_id NOT NULL without double-creating
    — safe because the per-line guard guarantees no JC references them yet).

    Idempotent per LINE (not per plan): refuses if the line already has
    non-deleted job cards, so a partial wizard-create + Approve can't double-
    create (Approve's own plan-level guard blocks the other direction).

    Returns {plan_id, plan_line_id, job_card_ids, count} or {error: ...}.
    MUST run inside an outer transaction (the router wraps us).
    """
    # ---- validate inputs -------------------------------------------------
    try:
        qk = float(qty_kg)
    except (TypeError, ValueError):
        qk = 0.0
    if qk <= 0:
        return {"error": "invalid_qty", "message": "Quantity (kg) must be greater than 0"}
    if not wip_steps:
        return {"error": "no_wip_steps", "message": "At least one WIP process is required"}
    if not pkg_floor or not str(pkg_floor).strip():
        return {"error": "missing_pkg_floor", "message": "A packaging floor is required"}

    qu = None
    if qty_units not in (None, ""):
        try:
            _v = float(qty_units)
            qu = _v if _v > 0 else None
        except (TypeError, ValueError):
            qu = None

    # ---- load the line + its plan ---------------------------------------
    line = await conn.fetchrow(
        """
        SELECT l.plan_line_id, l.plan_id, l.bom_id, l.fg_sku_name, l.customer_name,
               l.planned_qty_kg, p.entity, p.warehouse
        FROM   production_plan_line_v2 l
        JOIN   production_plan_v2 p ON p.plan_id = l.plan_id
        WHERE  l.plan_line_id = $1
        FOR    UPDATE OF l
        """,
        plan_line_id,
    )
    if not line:
        return {"error": "line_not_found"}
    # The FOR UPDATE above serializes concurrent partial-chain creates on the
    # SAME line: a second create blocks until the first commits, then reads the
    # updated carded total below — so two racing splits can't both pass the
    # balance guard and over-card the line.

    # ---- balance guard: split a line into multiple partial chains --------
    # A plan line may be carded in several partial chains over time (e.g. a
    # 450 kg line as 225 kg now + 225 kg later). The already-carded qty is the
    # Σ of each chain's HEAD card planned_qty_kg — exactly one head per chain
    # (prev_job_card_id IS NULL). We cap the total at the line's planned qty
    # (decision: cap-at-remaining) so splitting can't over-card the line.
    existing_heads = int(await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_v2 "
        "WHERE plan_line_id=$1 AND prev_job_card_id IS NULL AND deleted_at IS NULL",
        plan_line_id,
    ) or 0)
    carded_kg = float(await conn.fetchval(
        "SELECT COALESCE(SUM(planned_qty_kg), 0) FROM job_card_v2 "
        "WHERE plan_line_id=$1 AND prev_job_card_id IS NULL AND deleted_at IS NULL",
        plan_line_id,
    ) or 0)
    planned_kg = float(line["planned_qty_kg"] or 0)
    _TOL = 0.001
    if planned_kg <= 0:
        # No planned qty to split against (e.g. RM SO) — keep the original
        # single-chain rule so behaviour is unchanged for these lines.
        if existing_heads > 0:
            return {"error": "job_cards_already_exist", "count": existing_heads}
    else:
        remaining_kg = round(planned_kg - carded_kg, 3)
        if remaining_kg <= _TOL:
            return {"error": "line_fully_carded",
                    "planned_qty_kg": planned_kg, "carded_qty_kg": round(carded_kg, 3),
                    "message": "This line is already fully carded."}
        if qk > remaining_kg + _TOL:
            return {"error": "exceeds_balance",
                    "remaining_qty_kg": remaining_kg, "requested_qty_kg": qk,
                    "message": (f"Quantity {qk} kg exceeds the remaining "
                                f"{remaining_kg} kg balance for this line.")}

    plan_id       = line["plan_id"]
    bom_id        = line["bom_id"]
    factory       = line["warehouse"]
    entity        = line["entity"]
    fg_sku_name   = line["fg_sku_name"]
    customer_name = line["customer_name"]

    # ---- build the ordered step spec: WIP steps (as given) + Packaging ---
    def _stage_for(name: str) -> str:
        # Same derive as create_job_cards_from_plan's NOT-NULL fallback.
        return (name or "").strip().lower().replace(" ", "_") or "wip"

    steps_spec: list[dict] = []
    for s in wip_steps:
        proc = (s.get("process") or "").strip()
        steps_spec.append({
            "process_name": proc or "WIP",
            "stage":        _stage_for(proc),
            "floor":        (s.get("floor") or "").strip() or None,
            "sfg_output":   (s.get("sfg_output") or "").strip() or None,
        })
    # Packaging is the terminal Final-FG stage. Stamp an explicit 'packaging'
    # stage so is_packing_stage() recognises it for EGA (never string-derive it).
    steps_spec.append({
        "process_name": (str(pkg_process).strip() or "Packaging"),
        "stage":        "packaging",
        "floor":        str(pkg_floor).strip(),
        "sfg_output":   None,
    })
    n = len(steps_spec)

    # ---- step reconciliation: replace on the FIRST chain, append after ---
    # The FIRST chain replaces the line's snapshot steps with the wizard's
    # (safe: no JC references them yet). A SUBSEQUENT partial chain must NOT
    # delete the existing steps — they're RESTRICT-referenced by the earlier
    # chain's cards — so it appends its own step rows with CONTINUED step_order
    # (uq_pps_v2_line_order is UNIQUE on (plan_line_id, step_order)).
    chain_no = existing_heads + 1
    if existing_heads == 0:
        await conn.execute(
            "DELETE FROM production_plan_step_v2 WHERE plan_line_id=$1", plan_line_id,
        )
        step_order_base = 0
    else:
        step_order_base = int(await conn.fetchval(
            "SELECT COALESCE(MAX(step_order), 0) FROM production_plan_step_v2 "
            "WHERE plan_line_id=$1", plan_line_id,
        ) or 0)
    step_ids: list[int] = []
    for i, sp in enumerate(steps_spec):
        async def _insert_step(_order=step_order_base + i + 1, _sp=sp):
            return await conn.fetchval(
                """
                INSERT INTO production_plan_step_v2 (
                    step_id, plan_line_id, step_order, process_name, stage, floor
                ) VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING step_id
                """,
                new_short_time_id(), plan_line_id, _order,
                _sp["process_name"], _sp["stage"], _sp["floor"],
            )
        step_ids.append(await insert_with_pk_retry(conn, _insert_step))

    # ---- create one chained JC per step ---------------------------------
    jc_ids: list[int] = []
    prev_jc_id: int | None = None
    prev_output_code: str | None = None

    for idx, sp in enumerate(steps_spec):
        is_first = idx == 0
        is_last  = idx == n - 1

        input_kind  = 'RM' if is_first else 'SFG'
        output_kind = 'FG' if is_last  else 'WIP'
        output_code: str | None = None
        input_code:  str | None = prev_output_code   # opens with prev producer's code

        # A non-last step that declares an SFG output is an SFG producer.
        # (Blank → plain WIP / NULL code: the safe multi-step WIP case.)
        if not is_last and sp["sfg_output"]:
            output_kind = 'SFG'
            output_code = sp["sfg_output"]

        is_locked     = not is_first
        status        = 'unlocked' if is_first else 'locked'
        locked_reason = None if is_first else 'awaiting_previous_stage'

        # Chain suffix keeps job_card_number UNIQUE across partial chains on the
        # same line (job_card_v2_job_card_number_key). First chain keeps the
        # original format for backward-compat; 2nd+ chains get -B{chain_no}.
        chain_suffix = "" if chain_no == 1 else f"-B{chain_no}"
        jc_number = f"PLAN-{plan_id}-L{plan_line_id}-S{idx + 1}{chain_suffix}"
        batch_no  = _batch_number(plan_id, plan_line_id, idx + 1) + chain_suffix
        step_id   = step_ids[idx]

        # NOTE: column/value order kept identical to
        # create_job_cards_from_plan's INSERT — keep the two in sync.
        async def _insert_jc(
            _step_id=step_id, _jc_number=jc_number, _batch_no=batch_no,
            _step_number=idx + 1, _process=sp["process_name"], _stage=sp["stage"],
            _floor=sp["floor"], _input_kind=input_kind, _output_kind=output_kind,
            _is_locked=is_locked, _status=status, _locked_reason=locked_reason,
            _prev=prev_jc_id, _input_code=input_code, _output_code=output_code,
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
                    prev_job_card_id,
                    input_code, output_code
                ) VALUES (
                    $1, $2,
                    $3, $4, $5, $6,
                    $7, $8, $9,
                    $10, $11, $12,
                    $13, $14, $15,
                    $16, $17,
                    $18, $19, $20,
                    $21, $22, $23,
                    $24,
                    $25, $26
                )
                RETURNING job_card_id
                """,
                candidate, _jc_number,
                plan_id, plan_line_id, _step_id, bom_id,
                _step_number, _process, _stage,
                fg_sku_name, customer_name, _batch_no,
                qk, qu, 'KGS',
                _input_kind, _output_kind,
                factory, _floor, entity,
                _is_locked, _locked_reason, _status,
                _prev,
                _input_code, _output_code,
            )
        jc_id = await insert_with_pk_retry(conn, _insert_jc)
        jc_ids.append(jc_id)

        # RM + PM indents on stage 1 only, from the LINE's bom_id.
        if is_first and bom_id:
            await _materialise_indents(
                conn,
                job_card_id=jc_id,
                bom_id=bom_id,
                planned_qty_kg=qk,
                is_first_stage=True,
                fg_sku_name=fg_sku_name,
                planned_qty_units=qu,
            )

        # Bi-directional chain — set the next pointer on the prev row.
        if prev_jc_id is not None:
            await conn.execute(
                "UPDATE job_card_v2 SET next_job_card_id=$1 WHERE job_card_id=$2",
                jc_id, prev_jc_id,
            )

        prev_jc_id = jc_id
        prev_output_code = output_code

    logger.info(
        "Created %d job card(s) for plan_line_id=%d (plan_id=%d): dispatched to floors %s",
        len(jc_ids), plan_line_id, plan_id,
        [sp["floor"] for sp in steps_spec],
    )
    return {
        "plan_id": plan_id,
        "plan_line_id": plan_line_id,
        "job_card_ids": jc_ids,
        "count": len(jc_ids),
    }


async def create_merged_process_run(
    conn,
    *,
    plan_line_ids: list[int],
    wip_steps: list[dict],
    per_member: list[dict],
    created_by: str | None = None,
) -> dict:
    """Cross-product process merge: build ONE shared PROCESS chain (summed qty,
    producing a shared SFG) whose output feeds each product's OWN packaging card.

    The member lines must share (factory, entity, stage-1 floor, RM-article set)
    and be uncarded — RE-VALIDATED here against the DB, never trusted from the
    client. Every card created is stamped with one process_group_id.

    Structure built (mirrors create_job_cards_for_line's insert contract exactly
    so downstream lock/handoff/box-scan/PDF keep working):
      * PROCESS chain on the PRIMARY line — the wip_steps; stage-1 input=RM /
        unlocked; the LAST wip step promoted to an SFG producer carrying the
        shared group SFG code. planned_qty_kg = Σ member qty. The combined RM
        indent is materialised on stage-1 by summing EACH member's own bom RM
        (native _materialise_indents per member, RM-only) — exact, not a
        primary-bom approximation.
      * One PACKAGING card per member on its OWN line: prev = the last process
        card, input_code = the shared SFG, output=FG, status locked (awaiting the
        process handoff), planned_qty_kg = that member's qty. PM is materialised
        per member on its own packaging card (PM-only).

    The last process card's next_job_card_id is left NULL — a single scalar can't
    fan to N packagings — so close_batch records its output and SKIPS the 1:1
    auto-dispatch; distribution is done by dispatch_process_group. RM actual
    consumption books on the ONE process card (primary line): with a single
    physical process card, per-member RM costing is not separable — that is an
    accepted consequence of the "one process card" model.

    MUST run inside an outer transaction. Returns
    {process_group_id, primary_plan_line_id, process_job_card_ids, packaging:
    [{plan_line_id, job_card_id, ...}], count} or {error, message}.
    """
    # ---- validate shape --------------------------------------------------
    member_ids = [int(x) for x in (plan_line_ids or []) if x is not None]
    if len(member_ids) < 2:
        return {"error": "need_two_lines", "message": "Select at least two products to merge."}
    if not wip_steps:
        return {"error": "no_wip_steps", "message": "At least one shared process step is required."}
    pm_by_line: dict[int, dict] = {}
    for m in (per_member or []):
        try:
            plid = int(m.get("plan_line_id"))
        except (TypeError, ValueError):
            return {"error": "bad_member", "message": "Each member needs a plan_line_id."}
        pm_by_line[plid] = m
    if set(pm_by_line) != set(member_ids):
        return {"error": "member_mismatch",
                "message": "per_member must cover exactly the selected plan_line_ids."}

    # ---- re-validate eligibility against the DB (never trust the client) --
    rows = await conn.fetch(
        """
        SELECT l.plan_line_id, l.plan_id, l.bom_id, l.fg_sku_name, l.customer_name,
               l.planned_qty_kg,
               lower(trim(p.warehouse)) AS factory,
               lower(trim(p.entity))    AS entity, p.entity AS entity_raw,
               p.warehouse AS warehouse_raw,
               (SELECT lower(trim(s.floor)) FROM production_plan_step_v2 s
                 WHERE s.plan_line_id = l.plan_line_id ORDER BY s.step_order LIMIT 1) AS floor1,
               (SELECT string_agg(DISTINCT lower(trim(bl.material_sku_name)), ' | '
                                  ORDER BY lower(trim(bl.material_sku_name)))
                  FROM bom_line bl WHERE bl.bom_id = l.bom_id AND lower(bl.item_type)='rm') AS rm_fp,
               -- STARTED = any card past the editable locked/unlocked state.
               -- Carded-but-unstarted lines are mergeable; the merge rebuilds
               -- their (unstarted) cards. Only STARTED chains are refused.
               (SELECT COUNT(*) FROM job_card_v2 j
                 WHERE j.plan_line_id = l.plan_line_id AND j.deleted_at IS NULL
                   AND j.status NOT IN ('locked','unlocked')) AS jc_started
        FROM production_plan_line_v2 l
        JOIN production_plan_v2 p ON p.plan_id = l.plan_id
        WHERE l.plan_line_id = ANY($1::int[])
        FOR UPDATE OF l
        """,
        member_ids,
    )
    if len(rows) != len(member_ids):
        return {"error": "line_not_found", "message": "One or more selected lines no longer exist."}
    by_line = {r["plan_line_id"]: r for r in rows}
    loc = {(r["factory"], r["entity"], r["floor1"]) for r in rows}
    if len(loc) != 1 or any(k is None for k in next(iter(loc))):
        return {"error": "not_a_group",
                "message": "Selected lines don't share one factory + entity + stage-1 floor."}
    # Relaxed RM rule: members need only share AT LEAST ONE common RM article
    # (not the whole set). Intersect their RM sets; empty => not mergeable.
    rm_sets = [set((r["rm_fp"] or "").split(" | ")) - {""} for r in rows]
    if not (set.intersection(*rm_sets) if rm_sets else set()):
        return {"error": "no_common_rm",
                "message": "Selected lines share no common raw-material article."}
    started = [r["plan_line_id"] for r in rows if r["jc_started"]]
    if started:
        return {"error": "already_started",
                "message": (f"Lines have already-started job cards: {started}. Only "
                            "un-started chains can be merged.")}

    # ---- qty per member + shared totals ----------------------------------
    def _fq(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    member_qty: dict[int, float] = {}
    member_units: dict[int, float | None] = {}
    for plid in member_ids:
        m = pm_by_line[plid]
        qk = _fq(m.get("qty_kg"))
        planned = _fq(by_line[plid]["planned_qty_kg"])
        if qk <= 0:
            qk = planned  # default to the line's planned qty
        if planned > 0 and qk > planned + 0.001:
            return {"error": "exceeds_balance",
                    "message": f"Line {plid}: {qk} kg exceeds planned {planned} kg."}
        if not m.get("pkg_floor") or not str(m["pkg_floor"]).strip():
            return {"error": "missing_pkg_floor", "message": f"Line {plid} needs a packaging floor."}
        member_qty[plid] = qk
        u = m.get("qty_units")
        member_units[plid] = float(u) if u not in (None, "") and _fq(u) > 0 else None
    merged_qty = round(sum(member_qty.values()), 3)
    if merged_qty <= 0:
        return {"error": "invalid_qty", "message": "Merged quantity must be greater than 0."}

    primary = by_line[member_ids[0]]
    primary_line_id = primary["plan_line_id"]
    plan_id = primary["plan_id"]
    factory = primary["warehouse_raw"]
    entity  = primary["entity_raw"]
    group_id = new_short_time_id()

    # Shared SFG code the process chain produces and every packaging consumes.
    # Prefer the operator's last-step sfg_output; else the primary's routed seam.
    shared_sfg = (str(wip_steps[-1].get("sfg_output") or "").strip() or None)
    if not shared_sfg:
        shared_sfg = await _resolve_sfg_seam_code(conn, primary["bom_id"])
    # shared_sfg may be None (unrouted RM): packaging still binds to the process
    # card by prev_job_card_id, which the box-scan gate accepts on its own.

    def _stage_for(name: str) -> str:
        return (name or "").strip().lower().replace(" ", "_") or "wip"

    async def _insert_card(*, plan_line_id, plan_step_id, bom_id, step_number,
                           process_name, stage, floor, fg_sku_name, customer_name,
                           batch_number, qty_kg, qty_units, input_kind, output_kind,
                           is_locked, status, locked_reason, prev_jc, input_code,
                           output_code, job_card_number):
        async def _do():
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
                    prev_job_card_id,
                    input_code, output_code,
                    process_group_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23,
                    $24, $25, $26, $27
                )
                RETURNING job_card_id
                """,
                new_short_time_id(), job_card_number,
                plan_id, plan_line_id, plan_step_id, bom_id,
                step_number, process_name, stage,
                fg_sku_name, customer_name, batch_number,
                qty_kg, qty_units, 'KGS',
                input_kind, output_kind,
                factory, floor, entity,
                is_locked, locked_reason, status,
                prev_jc,
                input_code, output_code,
                group_id,
            )
        return await insert_with_pk_retry(conn, _do)

    async def _insert_step(plan_line_id, step_order, process_name, stage, floor):
        async def _do():
            return await conn.fetchval(
                """
                INSERT INTO production_plan_step_v2
                    (step_id, plan_line_id, step_order, process_name, stage, floor)
                VALUES ($1, $2, $3, $4, $5, $6) RETURNING step_id
                """,
                new_short_time_id(), plan_line_id, step_order, process_name, stage, floor,
            )
        return await insert_with_pk_retry(conn, _do)

    # Merge may re-shape UNSTARTED existing chains (carded-but-not-started
    # lines): hard-delete every member's cards first — matching
    # replace_job_cards_for_line — so the plan_step_id RESTRICT FK releases
    # before the steps are replaced. The `started` guard above guarantees no
    # running card is touched.
    await conn.execute(
        "DELETE FROM job_card_v2 WHERE plan_line_id = ANY($1::int[])", member_ids)

    # ---- build the shared PROCESS chain on the primary line --------------
    await conn.execute("DELETE FROM production_plan_step_v2 WHERE plan_line_id=$1", primary_line_id)
    n_wip = len(wip_steps)
    process_jc_ids: list[int] = []
    prev_jc_id: int | None = None
    prev_output_code: str | None = None
    for idx, s in enumerate(wip_steps):
        is_first = idx == 0
        is_last  = idx == n_wip - 1
        proc = (s.get("process") or "").strip() or "Process"
        floor = (s.get("floor") or "").strip() or None
        stage = _stage_for(proc)
        # Last process step is the SFG producer feeding every packaging; earlier
        # steps follow the normal declared-SFG-or-plain-WIP rule.
        if is_last:
            output_kind = 'SFG' if shared_sfg else 'WIP'
            output_code = shared_sfg
        elif (s.get("sfg_output") or "").strip():
            output_kind = 'SFG'
            output_code = (s.get("sfg_output") or "").strip()
        else:
            output_kind = 'WIP'
            output_code = None
        input_kind  = 'RM' if is_first else 'SFG'
        input_code  = prev_output_code
        step_id = await _insert_step(primary_line_id, idx + 1, proc, stage, floor)
        jc_id = await _insert_card(
            plan_line_id=primary_line_id, plan_step_id=step_id, bom_id=primary["bom_id"],
            step_number=idx + 1, process_name=proc, stage=stage, floor=floor,
            fg_sku_name=primary["fg_sku_name"], customer_name=primary["customer_name"],
            batch_number=_batch_number(plan_id, primary_line_id, idx + 1),
            qty_kg=merged_qty, qty_units=None,
            input_kind=input_kind, output_kind=output_kind,
            is_locked=not is_first, status='unlocked' if is_first else 'locked',
            locked_reason=None if is_first else 'awaiting_previous_stage',
            prev_jc=prev_jc_id, input_code=input_code, output_code=output_code,
            job_card_number=f"MPG-{group_id}-P{idx + 1}",
        )
        process_jc_ids.append(jc_id)
        if prev_jc_id is not None:
            await conn.execute(
                "UPDATE job_card_v2 SET next_job_card_id=$1 WHERE job_card_id=$2", jc_id, prev_jc_id)
        prev_jc_id = jc_id
        prev_output_code = output_code

    last_process_jc = process_jc_ids[-1]

    # ---- combined RM indent on stage-1: sum EACH member's own bom RM ------
    stage1_jc = process_jc_ids[0]
    for plid in member_ids:
        r = by_line[plid]
        if r["bom_id"]:
            await _materialise_indents(
                conn, job_card_id=stage1_jc, bom_id=r["bom_id"],
                planned_qty_kg=member_qty[plid], is_first_stage=True,
                fg_sku_name=r["fg_sku_name"], planned_qty_units=member_units[plid],
                include_rm=True, include_pm=False,
            )

    # ---- one PACKAGING card per member, fed by the shared process --------
    packaging: list[dict] = []
    for plid in member_ids:
        r = by_line[plid]
        m = pm_by_line[plid]
        pkg_floor = str(m["pkg_floor"]).strip()
        pkg_proc  = (str(m.get("pkg_process") or "").strip() or "Packaging")
        # The primary line's steps were already replaced above (process steps
        # 1..n_wip); its packaging is simply the next step_order. Non-primary
        # lines get their snapshot steps replaced with a single packaging step.
        if plid != primary_line_id:
            await conn.execute("DELETE FROM production_plan_step_v2 WHERE plan_line_id=$1", plid)
        pkg_order = (n_wip + 1) if plid == primary_line_id else 1
        step_id = await _insert_step(plid, pkg_order, pkg_proc, 'packaging', pkg_floor)
        jc_id = await _insert_card(
            plan_line_id=plid, plan_step_id=step_id, bom_id=r["bom_id"],
            step_number=pkg_order, process_name=pkg_proc, stage='packaging', floor=pkg_floor,
            fg_sku_name=r["fg_sku_name"], customer_name=r["customer_name"],
            batch_number=_batch_number(r["plan_id"], plid, pkg_order),
            qty_kg=member_qty[plid], qty_units=member_units[plid],
            input_kind='SFG', output_kind='FG',
            # Per the merge spec, packaging is created UNLOCKED (workable right
            # away) rather than awaiting the process handoff. dispatch_process_group
            # still carries qty + mints WIP into it (its unlock CASE just no-ops).
            is_locked=False, status='unlocked', locked_reason=None,
            prev_jc=last_process_jc, input_code=shared_sfg, output_code=None,
            job_card_number=f"MPG-{group_id}-PK{plid}",
        )
        # PM (packaging material) is per-product — materialise on THIS card.
        if r["bom_id"]:
            await _materialise_indents(
                conn, job_card_id=jc_id, bom_id=r["bom_id"],
                planned_qty_kg=member_qty[plid], is_first_stage=True,
                fg_sku_name=r["fg_sku_name"], planned_qty_units=member_units[plid],
                include_rm=False, include_pm=True,
            )
        packaging.append({
            "plan_line_id": plid, "job_card_id": jc_id,
            "fg_sku_name": r["fg_sku_name"], "qty_kg": member_qty[plid],
            "pkg_floor": pkg_floor, "pkg_process": pkg_proc,
        })

    logger.info(
        "Merged process run %s: %d process card(s) (%.3f kg) feeding %d packaging card(s) "
        "across lines %s (shared SFG=%s)",
        group_id, len(process_jc_ids), merged_qty, len(packaging), member_ids, shared_sfg,
    )
    return {
        "process_group_id": group_id,
        "primary_plan_line_id": primary_line_id,
        "merged_qty_kg": merged_qty,
        "shared_sfg_code": shared_sfg,
        "process_job_card_ids": process_jc_ids,
        "packaging": packaging,
        "count": len(process_jc_ids) + len(packaging),
    }


# A job card is still EDITABLE only while it hasn't started moving — i.e. it is
# in the initial locked/unlocked state with no material received / time logged.
# Once the floor begins (material_received, in_progress, …) the chain carries
# real work and must not be deleted/replaced.
_JC_EDITABLE_STATUSES = ('locked', 'unlocked')


async def get_line_job_card_config(conn, plan_line_id: int) -> dict:
    """Reconstruct the Create-Job-Card wizard payload from a line's EXISTING
    job cards, so the Edit flow can prefill the modal.

    Returns {exists, editable, started, qty_kg, qty_units, wip_steps[], pkg_floor}.
    `exists=False` when the line has no job cards (caller shows Create, not Edit).
    `editable=False` when any card has progressed beyond locked/unlocked.
    The last card in the chain is Packaging; the rest are the WIP processes.
    """
    # Canonical SFG for this line's FG (design §5.4) — used to auto-fill the
    # SFG output field in the Create/Edit Job-Card modal (stays editable).
    from app.modules.production.services.sfg_canonical import resolve_canonical_sfg_db
    meta = await conn.fetchrow(
        """
        SELECT l.fg_sku_name, p.entity
        FROM production_plan_line_v2 l
        JOIN production_plan_v2 p ON p.plan_id = l.plan_id
        WHERE l.plan_line_id = $1
        """,
        plan_line_id,
    )
    canonical_sfg = await resolve_canonical_sfg_db(
        conn,
        meta["fg_sku_name"] if meta else None,
        meta["entity"] if meta else None,
    )

    rows = await conn.fetch(
        """
        SELECT job_card_id, step_number, process_name, floor, output_code,
               planned_qty_kg, planned_qty_units, status
        FROM job_card_v2
        WHERE plan_line_id = $1 AND deleted_at IS NULL
        ORDER BY step_number
        """,
        plan_line_id,
    )
    if not rows:
        # No job cards yet → prefill the Create-Job-Card wizard from the plan's
        # snapshot route (production_plan_step_v2) so the operator can Create in
        # ONE click instead of re-typing the process/floor chain the plan already
        # knows. Last step = Packaging (Final FG); the rest are the WIP processes.
        # sfg_output is left NULL — the frontend seeds the canonical SFG.
        steps = await conn.fetch(
            """
            SELECT step_order, process_name, floor
            FROM production_plan_step_v2
            WHERE plan_line_id = $1
            ORDER BY step_order
            """,
            plan_line_id,
        )
        out = {"exists": False, "canonical_sfg": canonical_sfg}
        if steps:
            if len(steps) >= 2:
                wip_src, pkg_floor = steps[:-1], steps[-1]["floor"]
                pkg_process = steps[-1]["process_name"]
            else:  # single-step route: use it as the lone WIP, reuse its floor for packaging
                wip_src, pkg_floor = steps, steps[0]["floor"]
                pkg_process = "Packaging"
            out["wip_steps"] = [
                {"process": s["process_name"], "floor": s["floor"],
                 "sfg_output": None, "job_card_id": None, "started": False}
                for s in wip_src
            ]
            out["pkg_floor"] = pkg_floor
            out["pkg_process"] = pkg_process
        return out

    started = any(r["status"] not in _JC_EDITABLE_STATUSES for r in rows)
    wip_rows = rows[:-1]          # every stage except the terminating Packaging
    pkg_row  = rows[-1]
    first    = rows[0]
    return {
        "exists": True,
        "canonical_sfg": canonical_sfg,
        # `editable` keeps its original meaning (whole chain still replaceable
        # via the delete+recreate path). The live-edit path (apply-edits) works
        # even when started=True — the frontend uses per-step `started` to gate
        # which actions are allowed.
        "editable": not started,
        "started": started,
        "qty_kg": float(first["planned_qty_kg"]) if first["planned_qty_kg"] is not None else None,
        "qty_units": float(first["planned_qty_units"]) if first["planned_qty_units"] is not None else None,
        "wip_steps": [
            {
                "job_card_id": r["job_card_id"],
                "process": r["process_name"],
                "floor": r["floor"],
                "sfg_output": r["output_code"],
                "status": r["status"],
                "started": r["status"] not in _JC_EDITABLE_STATUSES,
            }
            for r in wip_rows
        ],
        "pkg_floor": pkg_row["floor"],
        "pkg_process": pkg_row["process_name"],
        "pkg_job_card_id": pkg_row["job_card_id"],
        "pkg_status": pkg_row["status"],
        "pkg_started": pkg_row["status"] not in _JC_EDITABLE_STATUSES,
    }


async def replace_job_cards_for_line(
    conn,
    plan_line_id: int,
    *,
    qty_kg,
    qty_units=None,
    wip_steps: list[dict],
    pkg_floor: str,
    pkg_process: str = "Packaging",
) -> dict:
    """Edit a line's job cards by REPLACING them: delete the current chain and
    recreate it from the wizard's inputs. Only valid while the chain is still
    fresh (every card locked/unlocked); refuses once any stage has started so
    in-progress work is never destroyed.

    Deleting the cards cascades their RM/PM indents (FK ON DELETE CASCADE); the
    plan_step rows are then rebuilt by create_job_cards_for_line. MUST run inside
    an outer transaction.
    """
    existing = await conn.fetch(
        "SELECT job_card_id, status FROM job_card_v2 "
        "WHERE plan_line_id = $1 AND deleted_at IS NULL",
        plan_line_id,
    )
    if not existing:
        # Nothing to replace — fall through to a plain create.
        return await create_job_cards_for_line(
            conn, plan_line_id, qty_kg=qty_kg, qty_units=qty_units,
            wip_steps=wip_steps, pkg_floor=pkg_floor, pkg_process=pkg_process,
        )

    if any(r["status"] not in _JC_EDITABLE_STATUSES for r in existing):
        return {
            "error": "not_editable",
            "message": "These job cards have already started — they can't be edited.",
        }

    # Drop the whole chain in one statement (indents cascade; the bidirectional
    # prev/next self-FK is satisfied because the entire referenced set goes too).
    await conn.execute(
        "DELETE FROM job_card_v2 WHERE plan_line_id = $1", plan_line_id,
    )
    # Recreate from the new inputs (its per-line guard now passes — no JC remains).
    result = await create_job_cards_for_line(
        conn, plan_line_id, qty_kg=qty_kg, qty_units=qty_units,
        wip_steps=wip_steps, pkg_floor=pkg_floor, pkg_process=pkg_process,
    )
    if "error" not in result:
        result["replaced"] = len(existing)
    return result


# ---------------------------------------------------------------------------
# Live (started-chain) edit — constrained mirror of Create
# ---------------------------------------------------------------------------
#
# replace_job_cards_for_line is all-or-nothing and refuses once any stage starts.
# apply_live_job_card_edits is the constrained alternative that works on a chain
# that has ALREADY started: floor + qty changes anytime, add a process in the
# un-started tail, and remove a process (un-started → snapshot+cancel; in-progress
# → force-record the JC's full data then cancel). Qty changes propagate to the
# linked SO (ledger + so_line) via sync_so_from_qty_delta. Every action is logged
# to job_card_edit_log_v2.

# Pre-start statuses (chain still freely replaceable).
_LIVE_TERMINAL = ('completed', 'closed', 'cancelled')


async def _force_record_and_cancel_jc(conn, *, job_card_id: int,
                                      reason: str, deleted_by: str | None) -> dict:
    """Snapshot a job card's FULL current data into cancelled_snapshot, close any
    open shift/batch, then soft-cancel it — the 'record the job-card data, then
    remove' path for a removed process. Works for any non-terminal status
    (un-started or in-progress). Rejects terminal JCs. Returns the snapshot.
    """
    import json
    jc = await conn.fetchrow(
        """SELECT status FROM job_card_v2
           WHERE job_card_id=$1 AND deleted_at IS NULL FOR UPDATE""",
        job_card_id,
    )
    if not jc:
        return {"error": "job_card_not_found"}
    if jc["status"] in _LIVE_TERMINAL:
        return {"error": "cannot_remove_terminal",
                "message": f"Cannot remove a process in '{jc['status']}' status."}

    # Snapshot mirrors GET /job-cards-v2/{id} so the cancelled JC keeps its real
    # outputs/batches/consumption even after the row is soft-deleted. A failure
    # here is fatal (rolls the txn back) — a silent drop would defeat the point.
    snapshot_payload = await get_job_card(conn, job_card_id)
    snapshot_json = json.dumps(snapshot_payload, default=str, ensure_ascii=False)
    rsn = (reason or "Removed via live edit").strip()

    # Close any open shift segment + open batch (no-op when un-started).
    await conn.execute(
        """UPDATE job_card_shift_log_v2
              SET end_at = NOW(),
                  notes  = COALESCE(notes || E'\n', '') || 'Closed by live-edit remove: ' || $2
            WHERE job_card_id = $1 AND end_at IS NULL""",
        job_card_id, rsn,
    )
    await conn.execute(
        """UPDATE job_card_batch_v2
              SET status='cancelled', ended_at=COALESCE(ended_at, NOW()),
                  closed_at=NOW(), closed_by=$2,
                  notes=COALESCE(notes || E'\n', '') || 'Cancelled by live-edit remove: ' || $3
            WHERE job_card_id = $1 AND status = 'open'""",
        job_card_id, deleted_by, rsn,
    )
    await conn.execute(
        """UPDATE job_card_v2
              SET status='cancelled', deleted_at=NOW(), deleted_by=$2,
                  cancellation_reason='[EDIT_REMOVE] ' || $3,
                  cancelled_snapshot=$4::jsonb
            WHERE job_card_id=$1""",
        job_card_id, deleted_by, rsn, snapshot_json,
    )
    return {"removed": True, "snapshot": snapshot_payload}


async def sync_so_from_qty_delta(conn, plan_line_id: int, *,
                                 delta_kg, delta_units,
                                 user: str | None = None, reason: str | None = None) -> dict:
    """Propagate a plan-line qty change to the linked Sales Order — BOTH the
    demand ledger (so_fulfillment_v2.planned_qty_*) AND the so_line rows shown on
    the SO-creation page. Signed delta: positive increments, negative decrements.
    Mirrors create_plan's ledger reserve (+= planned) and cancel_plan's release
    (GREATEST(0, ...)). Returns a sync summary for the audit log.
    """
    dk = float(delta_kg or 0)
    du = float(delta_units or 0)
    if abs(dk) < 1e-9 and abs(du) < 1e-9:
        return {"synced": False, "reason": "no_delta"}

    line = await conn.fetchrow(
        "SELECT linked_so_fulfillment_ids FROM production_plan_line_v2 WHERE plan_line_id=$1",
        plan_line_id,
    )
    fids = list(line["linked_so_fulfillment_ids"] or []) if line else []
    if not fids:
        return {"synced": False, "reason": "no_linked_fulfillments"}

    # ── Ledger: bump planned_qty on each linked fulfillment (mirror create_plan).
    try:
        await conn.execute(
            """UPDATE so_fulfillment_v2
                  SET planned_qty_kg    = GREATEST(0, planned_qty_kg    + $1),
                      planned_qty_units = GREATEST(0, planned_qty_units + $2)
                WHERE so_fulfillment_id = ANY($3)""",
            dk, du, fids,
        )
    except CheckViolationError as exc:
        # chk_pending_*_nonneg — the increase would over-allocate. Roll back.
        raise ValueError(
            f"Qty change would over-allocate fulfillment(s) {fids}: pending isn't "
            f"enough for {dk:+.3f} kg / {du:+.3f} pcs. "
            f"({getattr(exc, 'constraint_name', None)})"
        ) from exc

    for fid in fids:
        if abs(dk) >= 1e-9:
            await conn.execute(
                """INSERT INTO so_revision_log_v2
                       (so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
                   VALUES ($1, 'qty_change', NULL, $2, $3, $4)""",
                fid, f"{dk:+.3f} kg (planned)", reason or "Live job-card edit", user,
            )
        if abs(du) >= 1e-9:
            await conn.execute(
                """INSERT INTO so_revision_log_v2
                       (so_fulfillment_id, revision_type, old_value, new_value, reason, revised_by)
                   VALUES ($1, 'units_change', NULL, $2, $3, $4)""",
                fid, f"{du:+.3f} pcs (planned)", reason or "Live job-card edit", user,
            )

    # ── so_line writeback: resolve fulfillments → so_lines, apportion the delta.
    frows = await conn.fetch(
        """SELECT DISTINCT so_line_id FROM so_fulfillment_v2
           WHERE so_fulfillment_id = ANY($1) AND so_line_id IS NOT NULL""",
        fids,
    )
    so_line_ids = [r["so_line_id"] for r in frows]
    updated_lines: list[dict] = []
    if so_line_ids:
        lrows = await conn.fetch(
            "SELECT so_line_id, quantity, quantity_units FROM so_line WHERE so_line_id = ANY($1)",
            so_line_ids,
        )
        # Apportion proportionally to each line's current kg; equal split when all
        # are zero. so_line.quantity_units is INT (kg) so kg rounds; quantity (pcs)
        # is NUMERIC. Clamp at zero so a decrement can't go negative.
        total_kg = sum(float(r["quantity_units"] or 0) for r in lrows)
        nlines = len(lrows)
        for r in lrows:
            cur_kg = float(r["quantity_units"] or 0)
            cur_pcs = float(r["quantity"] or 0)
            share = (cur_kg / total_kg) if total_kg > 0 else (1.0 / nlines)
            new_kg = max(0, round(cur_kg + dk * share))
            new_pcs = max(0.0, cur_pcs + du * share)
            await conn.execute(
                "UPDATE so_line SET quantity_units=$1, quantity=$2 WHERE so_line_id=$3",
                int(new_kg), new_pcs, r["so_line_id"],
            )
            updated_lines.append({
                "so_line_id": r["so_line_id"],
                "new_kg": int(new_kg), "new_pcs": new_pcs,
            })

    return {
        "synced": True,
        "fulfillment_ids": fids,
        "so_line_ids": so_line_ids,
        "delta_kg": dk,
        "delta_units": du,
        "so_lines": updated_lines,
    }


async def apply_live_job_card_edits(
    conn, plan_line_id: int, *,
    qty_kg, qty_units=None,
    steps: list[dict],
    pkg_floor: str | None = None,
    pkg_process: str | None = None,
    pkg_job_card_id: int | None = None,
    user: str | None = None,
    remove_reasons: dict | None = None,
) -> dict:
    """Constrained live edit of a STARTED job-card chain. `steps` are the desired
    WIP processes in order; existing ones carry job_card_id, new ones have
    job_card_id=None. Any existing WIP JC absent from `steps` is a removal.
    Packaging stays the terminal stage. MUST run inside an outer transaction.

    Rules (the started cards form a contiguous prefix — stage 1 starts first):
      * floor change: allowed on any non-terminal card (incl. in-progress);
      * qty change: allowed anytime, synced to the SO (ledger + so_line);
      * add process: only in the un-started tail (after the started prefix);
      * remove process: terminal cards can't be removed; a started card can only
        be removed if it's the latest running stage (no started downstream); the
        removed card is force-recorded (snapshot) then cancelled.
    """
    import json
    EDITABLE = _JC_EDITABLE_STATUSES        # ('locked', 'unlocked')
    remove_reasons = remove_reasons or {}
    actor = user or None

    rows = await conn.fetch(
        """SELECT job_card_id, step_number, process_name, stage, floor, status,
                  planned_qty_kg, planned_qty_units, output_code, plan_step_id, plan_id
           FROM job_card_v2
           WHERE plan_line_id=$1 AND deleted_at IS NULL
           ORDER BY step_number""",
        plan_line_id,
    )
    if not rows:
        return {"error": "no_job_cards",
                "message": "This line has no job cards — use Create instead."}
    plan_id = rows[0]["plan_id"]
    wip_rows = list(rows[:-1])
    pkg_row = rows[-1]
    existing_by_id = {r["job_card_id"]: r for r in rows}

    submitted = list(steps or [])
    submitted_id_set = {s.get("job_card_id") for s in submitted if s.get("job_card_id")}
    wip_ids_order = [r["job_card_id"] for r in wip_rows]
    removed_ids = [jid for jid in wip_ids_order if jid not in submitted_id_set]

    # Started prefix = contiguous run of started (non-EDITABLE) WIP cards.
    started_prefix_ids: list[int] = []
    for r in wip_rows:
        if r["status"] in EDITABLE:
            break
        started_prefix_ids.append(r["job_card_id"])

    # ── validate removals ────────────────────────────────────────────────
    for jid in removed_ids:
        r = existing_by_id[jid]
        if r["status"] in _LIVE_TERMINAL:
            return {"error": "cannot_remove_terminal",
                    "message": f"Process '{r['process_name']}' is {r['status']} and can't be removed."}
        if started_prefix_ids and jid in started_prefix_ids and jid != started_prefix_ids[-1]:
            return {"error": "cannot_remove_started_midchain",
                    "message": (f"Process '{r['process_name']}' has started and has started "
                                "downstream stages — only the latest running stage can be removed.")}

    # ── validate the started region stays the leading prefix, in order ────
    surviving_started = [jid for jid in started_prefix_ids if jid not in removed_ids]
    leading = [s.get("job_card_id") for s in submitted[:len(surviving_started)]]
    if leading != surviving_started:
        return {"error": "cannot_reorder_started_region",
                "message": "Started processes must stay first and in order; only the un-started tail can change."}

    audit: list[tuple] = []   # (action, job_card_id, before, after, reason)

    # ── qty change + SO sync (computed against current, before structural ops)
    base_kg = wip_rows[0]["planned_qty_kg"] if wip_rows else pkg_row["planned_qty_kg"]
    base_units = wip_rows[0]["planned_qty_units"] if wip_rows else pkg_row["planned_qty_units"]
    cur_kg = float(base_kg) if base_kg is not None else 0.0
    cur_units = float(base_units) if base_units is not None else None
    try:
        new_kg = float(qty_kg)
    except (TypeError, ValueError):
        new_kg = cur_kg
    if new_kg <= 0:
        return {"error": "invalid_qty", "message": "Quantity (kg) must be greater than 0"}
    eff_units = None
    if qty_units not in (None, ""):
        try:
            _v = float(qty_units)
            eff_units = _v if _v > 0 else None
        except (TypeError, ValueError):
            eff_units = None

    so_sync = {"synced": False}
    delta_kg = new_kg - cur_kg
    delta_units = ((eff_units if eff_units is not None else (cur_units or 0)) - (cur_units or 0))
    if abs(delta_kg) > 1e-9 or abs(delta_units) > 1e-9:
        await conn.execute(
            """UPDATE job_card_v2
                  SET planned_qty_kg=$1,
                      planned_qty_units=COALESCE($2, planned_qty_units),
                      updated_by=$3, updated_at=NOW()
                WHERE plan_line_id=$4 AND deleted_at IS NULL
                  AND status NOT IN ('completed','closed','cancelled')""",
            new_kg, eff_units, actor, plan_line_id,
        )
        await conn.execute(
            """UPDATE production_plan_line_v2
                  SET planned_qty_kg=$1,
                      planned_qty_units=COALESCE($2, planned_qty_units)
                WHERE plan_line_id=$3""",
            new_kg, eff_units, plan_line_id,
        )
        so_sync = await sync_so_from_qty_delta(
            conn, plan_line_id, delta_kg=delta_kg, delta_units=delta_units,
            user=actor, reason=f"Live job-card edit (line {plan_line_id})",
        )
        audit.append(("qty_change", None,
                      {"qty_kg": cur_kg, "qty_units": cur_units},
                      {"qty_kg": new_kg, "qty_units": eff_units}, None))

    # ── removals: force-record + cancel ──────────────────────────────────
    for jid in removed_ids:
        rsn = remove_reasons.get(jid) or remove_reasons.get(str(jid)) or "Removed via live edit"
        res = await _force_record_and_cancel_jc(
            conn, job_card_id=jid, reason=rsn, deleted_by=actor)
        if res.get("error"):
            return res
        audit.append(("remove_process", jid, res.get("snapshot"), None, rsn))

    # ── additions: create new WIP JCs (locked/awaiting) in the un-started tail
    meta = await conn.fetchrow(
        """SELECT l.bom_id, l.fg_sku_name, l.customer_name, p.entity, p.warehouse
           FROM production_plan_line_v2 l
           JOIN production_plan_v2 p ON p.plan_id = l.plan_id
           WHERE l.plan_line_id = $1""",
        plan_line_id,
    )
    factory, entity = meta["warehouse"], meta["entity"]
    fg_sku_name, customer_name, bom_id = meta["fg_sku_name"], meta["customer_name"], meta["bom_id"]

    # New steps are inserted at a temporary high step_order (above every current
    # order on the line) so the INSERT can't collide with an existing survivor's
    # order; the final 1..n renumber happens in the two-pass below. The
    # uq_pps_v2_line_order constraint is DEFERRABLE INITIALLY IMMEDIATE, so we
    # must never pass through a duplicate order at any statement boundary.
    step_order_base = int(await conn.fetchval(
        "SELECT COALESCE(MAX(step_order), 0) FROM production_plan_step_v2 WHERE plan_line_id=$1",
        plan_line_id,
    ) or 0)

    final_wip_ids: list[int] = []
    step_for: dict[int, dict] = {}
    for pos, s in enumerate(submitted):
        jid = s.get("job_card_id")
        if jid:
            final_wip_ids.append(jid)
            step_for[jid] = s
            continue
        proc = (s.get("process") or "").strip() or "WIP"
        stage = proc.lower().replace(" ", "_") or "wip"
        floor = (s.get("floor") or "").strip() or None

        async def _ins_step(_p=proc, _st=stage, _fl=floor, _ord=step_order_base + 1 + pos):
            return await conn.fetchval(
                """INSERT INTO production_plan_step_v2
                       (step_id, plan_line_id, step_order, process_name, stage, floor)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING step_id""",
                new_short_time_id(), plan_line_id, _ord, _p, _st, _fl,
            )
        new_step_id = await insert_with_pk_retry(conn, _ins_step)

        async def _ins_jc(_sid=new_step_id, _p=proc, _st=stage, _fl=floor, _ord=pos + 1):
            return await conn.fetchval(
                """INSERT INTO job_card_v2 (
                       job_card_id, job_card_number, plan_id, plan_line_id, plan_step_id, bom_id,
                       step_number, process_name, stage, fg_sku_name, customer_name, batch_number,
                       planned_qty_kg, planned_qty_units, uom, input_kind, output_kind,
                       factory, floor, entity, is_locked, locked_reason, status
                   ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,'KGS','SFG','WIP',
                             $15,$16,$17,TRUE,'awaiting_previous_stage','locked')
                   RETURNING job_card_id""",
                new_short_time_id(), f"PLAN-{plan_id}-L{plan_line_id}-ADD{_ord}",
                plan_id, plan_line_id, _sid, bom_id,
                _ord, _p, _st, fg_sku_name, customer_name,
                _batch_number(plan_id, plan_line_id, _ord),
                new_kg, eff_units, factory, _fl, entity,
            )
        new_jc_id = await insert_with_pk_retry(conn, _ins_jc)
        final_wip_ids.append(new_jc_id)
        step_for[new_jc_id] = s
        audit.append(("add_process", new_jc_id, None, {"process": proc, "floor": floor}, None))

    if not final_wip_ids:
        return {"error": "no_wip_steps", "message": "At least one WIP process is required"}

    # ── relink the surviving + new chain (WIP order) + packaging ──────────
    full_chain = final_wip_ids + [pkg_row["job_card_id"]]
    n = len(full_chain)
    chain_rows = await conn.fetch(
        """SELECT job_card_id, status, floor, process_name, stage, output_code, plan_step_id
           FROM job_card_v2 WHERE job_card_id = ANY($1)""",
        full_chain,
    )
    by_id = {r["job_card_id"]: r for r in chain_rows}

    prev_output_code: str | None = None
    for i, jid in enumerate(full_chain):
        r = by_id[jid]
        is_first = i == 0
        is_last = i == n - 1
        prev_id = full_chain[i - 1] if i > 0 else None
        next_id = full_chain[i + 1] if i < n - 1 else None
        started = r["status"] not in EDITABLE       # started or terminal → preserve seam

        if is_last:
            desired_floor = (str(pkg_floor).strip() if pkg_floor else None) or r["floor"]
            # Allow renaming the terminal step (e.g. a merged "Sorting + Packing")
            # while keeping its packaging stage. Started/terminal cards preserve
            # their process below regardless (real material flow is untouched).
            desired_proc = (str(pkg_process).strip() if pkg_process else None) or r["process_name"]
            desired_stage = r["stage"]
            sfg_out = None
        else:
            s = step_for[jid]
            desired_floor = ((s.get("floor") or "").strip() or None) or r["floor"]
            desired_proc = (s.get("process") or "").strip() or r["process_name"]
            desired_stage = (desired_proc or "").strip().lower().replace(" ", "_") or "wip"
            sfg_out = (s.get("sfg_output") or "").strip() or None

        floor_changed = desired_floor != r["floor"] and r["status"] not in _LIVE_TERMINAL

        if started:
            # Preserve process/kinds/codes (real material flow); allow a floor
            # change unless terminal. Only reposition pointers + step number.
            floor_to_set = desired_floor if r["status"] not in _LIVE_TERMINAL else r["floor"]
            await conn.execute(
                """UPDATE job_card_v2
                      SET step_number=$1, prev_job_card_id=$2, next_job_card_id=$3,
                          floor=$4, updated_by=$5, updated_at=NOW()
                    WHERE job_card_id=$6""",
                i + 1, prev_id, next_id, floor_to_set, actor, jid,
            )
            await conn.execute(
                "UPDATE production_plan_step_v2 SET floor=$1 WHERE step_id=$2",
                floor_to_set, r["plan_step_id"],
            )
            prev_output_code = r["output_code"]
        else:
            output_code = sfg_out if (not is_last and sfg_out) else None
            output_kind = 'SFG' if output_code else ('FG' if is_last else 'WIP')
            input_kind = 'RM' if is_first else 'SFG'
            await conn.execute(
                """UPDATE job_card_v2
                      SET step_number=$1, prev_job_card_id=$2, next_job_card_id=$3,
                          floor=$4, process_name=$5, stage=$6,
                          input_kind=$7, output_kind=$8, input_code=$9, output_code=$10,
                          updated_by=$11, updated_at=NOW()
                    WHERE job_card_id=$12""",
                i + 1, prev_id, next_id, desired_floor, desired_proc, desired_stage,
                input_kind, output_kind, prev_output_code, output_code, actor, jid,
            )
            await conn.execute(
                """UPDATE production_plan_step_v2
                      SET process_name=$1, stage=$2, floor=$3
                    WHERE step_id=$4""",
                desired_proc, desired_stage, desired_floor, r["plan_step_id"],
            )
            prev_output_code = output_code

        if floor_changed:
            audit.append(("floor_change", jid, {"floor": r["floor"]}, {"floor": desired_floor}, None))

    # ── renumber plan_step.step_order: two-pass park→final so the DEFERRABLE
    # INITIALLY IMMEDIATE uq_pps_v2_line_order never sees a duplicate. Pass 1
    # parks EVERY step on the line (incl. removed ones) above the current max;
    # Pass 2 sets the surviving chain to 1..n (removed steps stay parked high,
    # out of the 1..n range). Mirrors plan_v2.reorder_steps.
    line_steps = await conn.fetch(
        "SELECT step_id FROM production_plan_step_v2 WHERE plan_line_id=$1", plan_line_id,
    )
    park = int(await conn.fetchval(
        "SELECT COALESCE(MAX(step_order), 0) FROM production_plan_step_v2 WHERE plan_line_id=$1",
        plan_line_id,
    ) or 0)
    for srow in line_steps:
        park += 1
        await conn.execute(
            "UPDATE production_plan_step_v2 SET step_order=$2 WHERE step_id=$1",
            srow["step_id"], park,
        )
    for i, jid in enumerate(full_chain):
        await conn.execute(
            "UPDATE production_plan_step_v2 SET step_order=$2 WHERE step_id=$1",
            by_id[jid]["plan_step_id"], i + 1,
        )

    # If the previously-running stage was removed, the new head may be a locked
    # card with nothing upstream to release it — unlock it so work can resume.
    head_id = full_chain[0]
    if by_id[head_id]["status"] == 'locked':
        await conn.execute(
            """UPDATE job_card_v2
                  SET status='unlocked', is_locked=FALSE, locked_reason=NULL, updated_at=NOW()
                WHERE job_card_id=$1 AND status='locked'""",
            head_id,
        )

    # ── audit log ─────────────────────────────────────────────────────────
    for action, jid, before, after, rsn in audit:
        so_blob = (json.dumps(so_sync, default=str)
                   if action == 'qty_change' and so_sync.get("synced") else None)
        await conn.execute(
            """INSERT INTO job_card_edit_log_v2
                   (edit_log_id, plan_id, plan_line_id, job_card_id, action,
                    before_value, after_value, so_sync, reason, edited_by)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10)""",
            new_short_time_id(), plan_id, plan_line_id, jid, action,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            so_blob, rsn, actor,
        )

    return {
        "plan_id": plan_id,
        "plan_line_id": plan_line_id,
        "job_card_ids": full_chain,
        "removed": len(removed_ids),
        "added": sum(1 for a in audit if a[0] == 'add_process'),
        "floors_changed": sum(1 for a in audit if a[0] == 'floor_change'),
        "qty_changed": any(a[0] == 'qty_change' for a in audit),
        "so_sync": so_sync,
    }


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


async def maybe_release_plan_from_jcs(conn, plan_id: int,
                                      approved_by: str | None = None) -> bool:
    """If EVERY line on this plan now has (non-deleted) job cards, flip a draft
    plan to 'approved'. The per-line Create-Job-Card wizard is the modern
    equivalent of the bulk Approve, so once all articles are carded via the
    wizard the plan should read as approved (released to the floor), not draft.

    Idempotent — only acts on a plan still in 'draft'; stamps approved_by /
    approved_at like the Approve button does (COALESCE so a real approval is
    never overwritten). Returns True when a transition happened.
    """
    plan = await conn.fetchrow(
        "SELECT status FROM production_plan_v2 WHERE plan_id=$1", plan_id,
    )
    if not plan or plan["status"] != 'draft':
        return False

    # A line is "carded" for auto-approval only when it is FULLY carded — the
    # Σ of its chains' head-card qty (prev_job_card_id IS NULL) reaches its
    # planned qty. A partially-carded line (e.g. 225 of 450 kg) must NOT flip
    # the plan to approved; the operator still owes the balance chain.
    counts = await conn.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM production_plan_line_v2 WHERE plan_id=$1) AS lines,
            (SELECT COUNT(*) FROM production_plan_line_v2 l
              WHERE l.plan_id=$1
                AND EXISTS (SELECT 1 FROM job_card_v2 j
                            WHERE j.plan_line_id=l.plan_line_id
                              AND j.deleted_at IS NULL)
                AND COALESCE((SELECT SUM(j.planned_qty_kg) FROM job_card_v2 j
                              WHERE j.plan_line_id=l.plan_line_id
                                AND j.prev_job_card_id IS NULL
                                AND j.deleted_at IS NULL), 0)
                    >= COALESCE(l.planned_qty_kg, 0) - 0.001) AS carded
        """,
        plan_id,
    )
    if not counts or counts["lines"] == 0 or counts["carded"] != counts["lines"]:
        return False

    await conn.execute(
        """
        UPDATE production_plan_v2
        SET status='approved',
            approved_by=COALESCE(approved_by, $2),
            approved_at=COALESCE(approved_at, NOW())
        WHERE plan_id=$1 AND status='draft'
        """,
        plan_id, approved_by,
    )
    logger.info("plan_id=%d auto-transitioned 'draft'->'approved' (all lines carded via wizard)", plan_id)
    return True


async def consolidate_plan_lines_for_merge(conn, primary_line_id: int,
                                           merge_line_ids: list[int]) -> dict | None:
    """Fold same-article sibling plan lines INTO the primary line so the wizard
    can build ONE job-card chain for the combined article (rule: same SKU in the
    same plan => one merged chain, both SO numbers on it).

    Sums planned_qty, UNIONs linked_so_fulfillment_ids (so every merged SO number
    surfaces on the chain via the existing get_job_card aggregation — no display
    change), UNIONs distinct customer_name, then DELETEs the siblings (their
    production_plan_step_v2 rows CASCADE away).

    Guards: every sibling must be on the SAME plan, SAME fg_sku_name AND SAME
    bom_id as the primary, and NONE of the lines (primary or siblings) may already
    have job cards (else the merge would double-produce and the delete would be
    blocked by the job_card_v2 -> line RESTRICT FK).

    Reservations are deliberately NOT touched — they were made per-fulfillment at
    plan-create and stay correct at rest.
    # ponytail: cancel_plan/delete_plan/sync_so_from_qty_delta still subtract a
    # line's FULL qty from every linked fulfillment (WHERE so_fulfillment_id =
    # ANY(fids)), so a merged (multi-fid) line over-releases a fulfillment that is
    # ALSO reserved by another plan. Pre-existing multi-fid ledger gap; fix by
    # teaching those three loops to apportion per fulfillment.

    Returns None on success (or when merge_line_ids is empty), else {"error", ...}.
    MUST run inside an outer transaction.
    """
    merge_line_ids = [m for m in (merge_line_ids or []) if m != primary_line_id]
    if not merge_line_ids:
        return None

    ids = [primary_line_id, *merge_line_ids]
    rows = await conn.fetch(
        """
        SELECT plan_line_id, plan_id, fg_sku_name, bom_id, customer_name,
               planned_qty_kg, planned_qty_units, linked_so_fulfillment_ids
        FROM production_plan_line_v2
        WHERE plan_line_id = ANY($1)
        """,
        ids,
    )
    by_id = {r["plan_line_id"]: r for r in rows}
    primary = by_id.get(primary_line_id)
    if primary is None or any(m not in by_id for m in merge_line_ids):
        return {"error": "line_not_found", "message": "A plan line to merge was not found"}

    pname = (primary["fg_sku_name"] or "").strip().lower()
    for m in merge_line_ids:
        s = by_id[m]
        if s["plan_id"] != primary["plan_id"]:
            return {"error": "merge_conflict", "message": "Cannot merge lines from different plans"}
        if (s["fg_sku_name"] or "").strip().lower() != pname:
            return {"error": "merge_conflict", "message": "Only lines with the same article can be merged"}
        if s["bom_id"] != primary["bom_id"]:
            return {"error": "merge_conflict", "message": "Lines have different BOMs — cannot merge"}

    carded = await conn.fetchval(
        "SELECT COUNT(*) FROM job_card_v2 WHERE plan_line_id = ANY($1) AND deleted_at IS NULL",
        ids,
    )
    if carded and carded > 0:
        return {"error": "already_carded",
                "message": "One of the articles already has job cards — cancel them before merging"}

    # union fids (dedup, order-stable), sum qty, union distinct customers
    fids: list[int] = []
    seen_f: set[int] = set()
    custs: list[str] = []
    seen_c: set[str] = set()
    sum_kg = 0.0
    sum_units = 0.0
    for r in rows:
        for f in (r["linked_so_fulfillment_ids"] or []):
            if f not in seen_f:
                seen_f.add(f); fids.append(f)
        c = (r["customer_name"] or "").strip()
        if c and c.lower() not in seen_c:
            seen_c.add(c.lower()); custs.append(c)
        sum_kg += float(r["planned_qty_kg"] or 0)
        sum_units += float(r["planned_qty_units"] or 0)

    await conn.execute(
        """
        UPDATE production_plan_line_v2
        SET linked_so_fulfillment_ids = $2,
            planned_qty_kg            = $3,
            planned_qty_units         = $4,
            customer_name             = $5
        WHERE plan_line_id = $1
        """,
        primary_line_id, fids, round(sum_kg, 3), round(sum_units, 3),
        (", ".join(custs) if custs else None),
    )
    await conn.execute(
        "DELETE FROM production_plan_line_v2 WHERE plan_line_id = ANY($1)",
        merge_line_ids,
    )
    logger.info("merged plan lines %s into primary line %d (%d fulfillments, %.3f kg)",
                merge_line_ids, primary_line_id, len(fids), sum_kg)
    return None


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
_JC_PENDENCY_CHIPS = frozenset({"overdue", "due_today", "due_this_week", "future", "pending_signoff"})


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
    elif pendency == "pending_signoff":
        # Completed production still awaiting the required production_head
        # signature — the JC can't be closed until it lands (see
        # close_job_card / REQUIRED_SIGN_OFFS).
        pendency_predicate = (
            "jc.status = 'completed' AND NOT EXISTS ("
            "  SELECT 1 FROM job_card_sign_off_v2 s "
            "   WHERE s.job_card_id = jc.job_card_id AND s.role = 'production_head')"
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
               -- SFG seam codes (Slice 7): the SFG#### identifiers on the
               -- chain edge. output_code is the SFG this JC produces (set on
               -- Create-WIP / intermediate stages); input_code is the SFG this
               -- JC consumes (set on downstream Final-FG stages). Additive
               -- fields the list UI surfaces as `SFGxxxx` on Create-WIP rows.
               jc.input_code, jc.output_code,
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
               -- SFG seam codes (Slice 7): the SFG#### identifiers on the
               -- chain edge. output_code is the SFG this JC produces (set on
               -- Create-WIP / intermediate stages); input_code is the SFG this
               -- JC consumes (set on downstream Final-FG stages). Additive
               -- fields the list UI surfaces as `SFGxxxx` on Create-WIP rows.
               jc.input_code, jc.output_code,
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

async def materialise_wip_dispatch(conn, *, producer_job_card_id: int,
                                   consumer_job_card_id: int | None,
                                   sfg_code: str | None, fg_sku_name: str | None,
                                   qty_kg: float, dispatch_id: int | None,
                                   entity: str | None,
                                   recorded_by: str | None = None) -> str | None:
    """Slice 5 — materialise a Create-WIP → Final-FG handoff EXACTLY ONCE per
    dispatch event. Shared by close_batch (auto-dispatch on close) and
    dispatch_to_next (manual) so both paths have identical side effects:

      (1) a WIP `inventory_batch` (item_type='wip', sku = the SFG#### code so the
          Stage-2 picker / get_sfg_on_hand / chain seam all agree, qty = dispatched,
          expiry = mfg + WIP_SHELF_LIFE_DAYS) — the physical stock the next stage issues from;
      (2) a synthetic SFG consumption row on the CONSUMER JC linked to the
          dispatch (source_dispatch_id) — pre-records the opening SFG input + makes
          the chain auditable. The mass-balance input for the consumer is its
          carried_qty_kg (set by the caller), NOT this row, so there's no double count;
      (3) a floor_movement audit (production_floor → wip_store).

    Returns the new WIP batch_id, or None when there's nothing to materialise
    (qty ≤ 0 or no resolvable sku). MUST run inside the caller's transaction.
    Idempotency is per-dispatch: call once per dispatch event (each dispatch mints
    its own batch); the synthetic-consumption upsert is keyed so a repeat is safe.
    """
    from app.modules.production.services.inventory_service import create_wip_batch
    if not qty_kg or qty_kg <= 0:
        return None
    sku = sfg_code or fg_sku_name
    if not sku:
        return None

    # Audit NULL-OUTPUT-CODE-CORRUPTION: minting under fg_sku_name (the fallback
    # when sfg_code is NULL) is only correct for a genuine non-SFG WIP handoff.
    # If this is actually a dropped SFG seam, the WIP lands under the FG name and
    # the downstream picker can't find it. dispatch_to_next hard-fails the
    # declared-SFG case before reaching here; this warning surfaces the remaining
    # ambiguous case (WIP output with no code) so a mis-routed chain is visible
    # rather than silent.
    if not sfg_code:
        logger.warning(
            "materialise_wip_dispatch: producer JC %s has no SFG output_code — "
            "minting WIP under FG sku '%s' (no distinct SFG identity). Verify this "
            "is a non-SFG WIP chain, not a dropped seam.",
            producer_job_card_id, fg_sku_name,
        )

    # (1) WIP stock
    wip_batch_id = await create_wip_batch(
        conn, sku_name=sku, qty_kg=qty_kg, entity=entity,
        job_card_id=producer_job_card_id, floor_id='wip_store',
        performed_by=recorded_by,
    )

    # (2) synthetic downstream consumption — AUDIT ONLY. actual_consumed_qty=0
    # so it never inflates any mass-balance input sum: the consumer's chain input
    # is its carried_qty_kg (set by the caller), and double-counting carried_in +
    # this row was the Slice-5-review conservation bug (roll-up multi-count /
    # close-modal). The row's value is the source_dispatch_id breadcrumb linking
    # the consumer back to the dispatch that fed it (+ the SFG#### identity).
    if consumer_job_card_id is not None:
        await upsert_consumption_lines(
            conn, job_card_id=consumer_job_card_id,
            entries=[{
                "material_sku_name": sku, "consumed_qty": 0, "uom": "KGS",
                "input_kind": "SFG", "source_dispatch_id": dispatch_id,
                "bom_line_id": None,
            }],
            input_kind="SFG", recorded_by=recorded_by,
        )

    # (3) floor_movement audit. job_card_id is left NULL: floor_movement.job_card_id
    # FKs the legacy v1 job_card table, so a v2 id would violate it. The producing
    # v2 JC is recorded in the reason text for traceability. A direct INSERT (not
    # move_material) because the WIP is newly produced — there's no pre-existing
    # production_floor stock for move_material to debit.
    await conn.execute(
        """
        INSERT INTO floor_movement
            (sku_name, from_location, to_location, quantity_kg, reason, entity, moved_by)
        VALUES ($1, 'production_floor', 'wip_store', $2, $3, $4, $5)
        """,
        sku, qty_kg, f"wip_production jc={producer_job_card_id}", entity, recorded_by,
    )
    return wip_batch_id


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
               dispatched_to_next_kg, output_kind, status,
               entity, output_code, fg_sku_name
        FROM   job_card_v2
        WHERE  job_card_id=$1 AND deleted_at IS NULL
        FOR    UPDATE
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
    # Slice-5 review #1: cap cumulative dispatch at produced output so a manual
    # dispatch on top of close_batch's auto-dispatch can't mint phantom WIP
    # (total dispatched > produced). Mirrors the legacy v1 engine's invariant
    # (dispatched_to_next_kg + qty <= produced). FOR UPDATE above serialises
    # concurrent dispatches so the check can't race.
    if (src["status"] or "") in ('closed', 'cancelled'):
        return {"error": "jc_terminal",
                "message": f"Cannot dispatch from a {src['status']} job card"}
    produced = float(await conn.fetchval(
        "SELECT COALESCE(SUM(output_qty_kg), 0) FROM job_card_output_v2 WHERE job_card_id=$1",
        job_card_id,
    ) or 0)
    already = float(src["dispatched_to_next_kg"] or 0)
    if produced > 0 and already + qty_kg > produced + 1e-6:
        return {"error": "over_dispatch",
                "message": (f"Dispatching {qty_kg} would exceed produced output: "
                            f"{already} already dispatched of {produced} produced."),
                "produced_kg": produced, "already_dispatched_kg": already}

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

    # Slice 5: materialise the dispatched SFG (WIP batch + synthetic consumption +
    # floor_movement). output_kind here is always SFG/WIP (FG returned early above).
    wip_batch_id = None
    if (src["output_kind"] or "").upper() in ('SFG', 'WIP'):
        # Defensive (audit NULL-OUTPUT-CODE-CORRUPTION): a step explicitly
        # declared as an SFG producer MUST carry its SFG#### output_code. A NULL
        # here means the seam was never stamped — e.g. a routed 2-stage article
        # whose plan diverged from the expected 2-step shape, which
        # create_job_cards_from_plan deliberately skips (codes left NULL). Without
        # this guard materialise_wip_dispatch falls back to `sfg_code or
        # fg_sku_name` and mints WIP stock under the FINISHED-GOODS sku name —
        # stock the Stage-2 picker / get_sfg_on_hand can never find. Fail loudly
        # rather than silently corrupt inventory.
        if (src["output_kind"] or "").upper() == 'SFG' and not src["output_code"]:
            raise ValueError(
                f"dispatch_to_next: JC {job_card_id} is an SFG producer "
                f"(output_kind=SFG) with a NULL output_code — refusing to "
                f"dispatch. The SFG seam was not stamped at JC creation."
            )
        wip_batch_id = await materialise_wip_dispatch(
            conn,
            producer_job_card_id=job_card_id,
            consumer_job_card_id=src["next_job_card_id"],
            sfg_code=src["output_code"], fg_sku_name=src["fg_sku_name"],
            qty_kg=qty_kg, dispatch_id=audit["dispatch_id"],
            entity=src["entity"], recorded_by=dispatched_by,
        )
    return {"dispatched": True, "dispatch": _serialize(audit),
            "wip_batch_id": wip_batch_id}


async def dispatch_process_group(conn, *, process_job_card_id: int,
                                 dispatched_by: str | None = None) -> dict:
    """Fan-out handoff for a merged process run (create_merged_process_run):
    distribute the shared PROCESS card's produced SFG to EVERY member packaging
    card hanging off it (prev_job_card_id = this card). Mirrors dispatch_to_next /
    close_batch's per-consumer side-effects EXACTLY — partial-dispatch audit,
    the producer's dispatched_to_next_kg, the consumer's carried_qty_kg + unlock,
    and one materialise_wip_dispatch (WIP batch + synthetic consumption +
    floor_movement) per consumer — but loops N consumers instead of the single
    next_job_card_id (which the merged process card leaves NULL).

    Qty split: each packaging receives its own planned_qty_kg, scaled down
    proportionally when the process produced less than the group total (loss),
    and capped so cumulative dispatch never exceeds produced. A packaging already
    fed (carried_qty_kg > 0) is skipped, so a re-run only tops up the rest.

    MUST run inside an outer transaction.
    """
    src = await conn.fetchrow(
        """
        SELECT job_card_id, output_kind, output_code, fg_sku_name, entity,
               status, dispatched_to_next_kg, process_group_id
        FROM   job_card_v2
        WHERE  job_card_id=$1 AND deleted_at IS NULL
        FOR    UPDATE
        """,
        process_job_card_id,
    )
    if not src:
        return {"error": "job_card_not_found"}
    if src["process_group_id"] is None:
        return {"error": "not_a_group",
                "message": "This card is not part of a merged process group."}
    if (src["output_kind"] or "").upper() not in ('SFG', 'WIP'):
        return {"error": "not_a_producer",
                "message": "This card is not an SFG/WIP producer — nothing to fan out."}
    if (src["output_kind"] or "").upper() == 'SFG' and not src["output_code"]:
        return {"error": "missing_seam",
                "message": "SFG producer has a NULL output_code; refusing to dispatch."}
    if (src["status"] or "") == 'cancelled':
        return {"error": "jc_terminal", "message": "Process card is cancelled."}

    produced = float(await conn.fetchval(
        "SELECT COALESCE(SUM(output_qty_kg),0) FROM job_card_output_v2 WHERE job_card_id=$1",
        process_job_card_id,
    ) or 0)
    if produced <= 0:
        return {"error": "no_output",
                "message": "Record the process output before distributing to packaging."}

    consumers = await conn.fetch(
        """
        SELECT job_card_id, plan_line_id, planned_qty_kg, planned_qty_units,
               carried_qty_kg, status, locked_reason
        FROM   job_card_v2
        WHERE  prev_job_card_id=$1 AND deleted_at IS NULL
        ORDER  BY job_card_id
        FOR    UPDATE
        """,
        process_job_card_id,
    )
    if not consumers:
        return {"error": "no_consumers",
                "message": "No packaging cards hang off this process card."}

    # Only feed consumers not yet fed. Split the remaining produced budget across
    # their planned qty proportionally, capping cumulative dispatch at produced.
    pending = [c for c in consumers if float(c["carried_qty_kg"] or 0) <= 0]
    if not pending:
        return {"dispatched": True, "process_group_id": src["process_group_id"],
                "produced_kg": produced, "results": [],
                "message": "All packaging cards already fed."}
    demand  = sum(float(c["planned_qty_kg"] or 0) for c in pending)
    already = float(src["dispatched_to_next_kg"] or 0)
    budget  = max(0.0, produced - already)
    scale   = min(1.0, (budget / demand) if demand > 0 else 0.0)

    results: list[dict] = []
    for c in pending:
        want = round(float(c["planned_qty_kg"] or 0) * scale, 3)
        if want <= 0:
            continue
        cu = c["planned_qty_units"]

        async def _ins(_to=c["job_card_id"], _q=want, _u=cu):
            return await conn.fetchrow(
                """
                INSERT INTO job_card_partial_dispatch_v2
                    (dispatch_id, from_job_card_id, to_job_card_id, qty_kg, qty_units,
                     dispatched_by, notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                new_short_time_id(), process_job_card_id, _to, _q, _u,
                dispatched_by, f"Merged process fan-out (group {src['process_group_id']})",
            )
        audit = await insert_with_pk_retry(conn, _ins)

        await conn.execute(
            "UPDATE job_card_v2 SET dispatched_to_next_kg = dispatched_to_next_kg + $1 "
            "WHERE job_card_id=$2",
            want, process_job_card_id,
        )
        await conn.execute(
            """
            UPDATE job_card_v2
               SET carried_qty_kg = carried_qty_kg + $1,
                   is_locked      = CASE WHEN locked_reason='awaiting_previous_stage' THEN FALSE ELSE is_locked END,
                   status         = CASE WHEN status='locked' AND locked_reason='awaiting_previous_stage'
                                         THEN 'unlocked' ELSE status END,
                   locked_reason  = CASE WHEN locked_reason='awaiting_previous_stage' THEN NULL ELSE locked_reason END
             WHERE job_card_id=$2
            """,
            want, c["job_card_id"],
        )
        wip_batch_id = await materialise_wip_dispatch(
            conn,
            producer_job_card_id=process_job_card_id,
            consumer_job_card_id=c["job_card_id"],
            sfg_code=src["output_code"], fg_sku_name=src["fg_sku_name"],
            qty_kg=want, dispatch_id=audit["dispatch_id"],
            entity=src["entity"], recorded_by=dispatched_by,
        )
        results.append({
            "packaging_job_card_id": c["job_card_id"],
            "plan_line_id": c["plan_line_id"], "qty_kg": want,
            "wip_batch_id": wip_batch_id,
        })

    return {"dispatched": True, "process_group_id": src["process_group_id"],
            "produced_kg": produced, "results": results}


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
                        process_loss_remark: str | None = None,
                        recorded_by: str | None = None,
                        batch_id: int | None = None) -> dict:
    """Append an output row for this JC. The output_kind defaults to the
    JC's declared output_kind (SFG / WIP / FG from the stage chain) unless
    overridden — e.g. when the floor reports a partial FG batch that's
    actually still WIP because QC failed.

    yield_pct is computed server-side as (output / rm_consumed) × 100 when
    rm_consumed > 0; NULL otherwise. The JC's status is NOT auto-flipped
    here — explicit /complete and /close calls handle that.

    `batch_id` tags the row with the batch it belongs to (migration 036).
    Sibling tables (consumption_lines, byproducts, balance_materials) all
    already tag their rows; without this column being written here too,
    the JC form's batchScopedDefaults can't fall back to the latest
    output row when the BatchRow snapshot is null, so FG Actual Kg /
    Process Loss appear blank after reload on open batches.
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
                (output_id, job_card_id, batch_id, rm_consumed_kg, output_qty_kg,
                 output_qty_units, output_kind, uom, yield_pct, notes,
                 recorded_by, process_loss_kg, process_loss_remark)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, batch_id, rm_consumed_kg, output_qty_kg,
            output_qty_units, kind, uom, yield_pct, notes,
            recorded_by, process_loss_kg or 0, process_loss_remark,
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

    # Auto-open a batch so the Output & Accounting form is writable the moment
    # START lands (status is now 'in_progress', which clears the FE's lifecycle
    # gate). reuse_if_open=True makes it a no-op when an open batch already
    # exists (out-of-band open / re-entry) so we never stack phantom opens. This
    # runs in the /start endpoint's existing transaction, so a JC can never
    # commit 'in_progress' with no batch — an open failure rolls back the status
    # flip too. Local import avoids a job_card_v2 <-> job_card_batch_v2 cycle.
    from app.modules.production.services.job_card_batch_v2 import open_batch
    batch_res = await open_batch(conn, job_card_id=job_card_id, reuse_if_open=True)
    if batch_res.get("error"):
        return batch_res

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
        # Input-side consumption sum: RM + SFG/WIP opening inputs (only PM is
        # excluded — packaging doesn't convert into FG mass). This matches the FE
        # SummaryCard rule (item_type !== 'PM'); an earlier RM-only filter dropped
        # the whole SFG input to 0 on a pack-of-existing-SFG (archetype C) card
        # whose only input is the SFG (Slice-5 review #5).
        rm_consumption = float(await conn.fetchval(
            """
            SELECT COALESCE(SUM(actual_consumed_qty), 0)
            FROM   job_card_material_consumption_v2
            WHERE  job_card_id = $1
              AND  COALESCE(input_kind, 'RM') <> 'PM'
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
    # FOR UPDATE locks the accounting rows so a concurrent B4 save can't
    # flip is_balanced between this read and the JC status UPDATE below.
    #
    # Migration 049: job_card_accounting_v2 is now per-(JC, batch). A JC
    # is closeable iff EVERY existing row reads is_balanced = true.
    # bool_and short-circuits in PG so an unbalanced batch fails the
    # aggregate; SUM(diff) surfaces the cumulative variance for the
    # error envelope. NULL is_balanced (legacy rows pre-B4) treated as
    # FALSE conservatively — must save accounting once before closing.
    # When no rows exist (fresh JC, COUNT(*)=0), trip the self-heal path.
    #
    # PG quirk: `FOR UPDATE` and aggregate functions can't appear in the
    # same query (PG errors with "FOR UPDATE is not allowed with
    # aggregate functions"). Lock the rows in a CTE first, then
    # aggregate over the CTE result — the row-locks acquired in the CTE
    # carry through to the outer query.
    acct = await conn.fetchrow(
        """
        WITH locked AS (
            SELECT is_balanced, balance_difference_qty
            FROM   job_card_accounting_v2
            WHERE  job_card_id = $1
            FOR    UPDATE
        )
        SELECT BOOL_AND(COALESCE(is_balanced, FALSE)) AS is_balanced,
               COALESCE(SUM(balance_difference_qty), 0) AS balance_difference_qty,
               COUNT(*) AS row_count
        FROM   locked
        """,
        job_card_id,
    )
    # COUNT(*) = 0 means no rows yet — trip the self-heal path below.
    if acct is not None and acct["row_count"] == 0:
        acct = None
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
        # gate runs on a stable snapshot. Same CTE-then-aggregate pattern
        # as above — PG forbids FOR UPDATE alongside aggregates in one
        # query, so the lock lives in a CTE and the aggregation happens
        # over the locked snapshot.
        acct = await conn.fetchrow(
            """
            WITH locked AS (
                SELECT is_balanced, balance_difference_qty
                FROM   job_card_accounting_v2
                WHERE  job_card_id = $1
                FOR    UPDATE
            )
            SELECT BOOL_AND(COALESCE(is_balanced, FALSE)) AS is_balanced,
                   COALESCE(SUM(balance_difference_qty), 0) AS balance_difference_qty,
                   COUNT(*) AS row_count
            FROM   locked
            """,
            job_card_id,
        )
        if acct is not None and acct["row_count"] == 0:
            acct = None
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
    changed_keys: list[str] = []
    idx = 1
    for k, v in (fields or {}).items():
        if k not in _PATCH_ALLOWED_COLUMNS:
            continue
        sets.append(f'"{k}" = ${idx}')
        params.append(v); idx += 1
        changed_keys.append(k)
    if not sets:
        return {"error": "no_change",
                "message": "No editable fields supplied"}
    # Snapshot pre-edit values of exactly the columns being patched (keys are from
    # the allow-list, never user-controlled → safe to interpolate) so the edit log
    # records real before→after per header field. Drives the FE red markers.
    cols = ", ".join(f'"{k}"' for k in changed_keys)
    old = await conn.fetchrow(
        f"SELECT {cols} FROM job_card_v2 WHERE job_card_id=$1", job_card_id,
    )
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
    if updated_by and old is not None:
        from app.modules.production.services.amendment_service import log_jc_field_changes
        await log_jc_field_changes(
            conn, job_card_id=job_card_id, record_type="job_card", field_prefix="",
            changed_by=updated_by, reason="header edit",
            before={k: old[k] for k in changed_keys},
            after={k: row[k] for k in changed_keys},
        )
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
    # G4 bar-line override (migration 061): surfaced so the FE can BADGE a
    # bar-line FG and show whether its route was re-derived from bar_line_process.
    bom_bar_line_process: str | None = None
    bom_bar_line_routed:  bool | None = None
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
        # G4: bar_line_process / bar_line_routed (migration 061) are additive —
        # on a server running 061-aware code BEFORE the migration lands, asyncpg
        # raises UndefinedColumnError; fall back to the pre-061 projection with
        # those two surfaced as NULL so JC detail stays loadable (mirrors the
        # batch_id fallback pattern above).
        try:
            bom_row = await conn.fetchrow(
                """
                SELECT item_group, pack_size_kg, version,
                       bar_line_process, bar_line_routed
                FROM   bom_header
                WHERE  bom_id = $1
                """,
                h["bom_id"],
            )
        except UndefinedColumnError:
            bom_row = await conn.fetchrow(
                """
                SELECT item_group, pack_size_kg, version,
                       NULL::TEXT AS bar_line_process,
                       NULL::BOOLEAN AS bar_line_routed
                FROM   bom_header
                WHERE  bom_id = $1
                """,
                h["bom_id"],
            )
        if bom_row:
            bom_item_group = bom_row["item_group"]
            bom_pack_size  = float(bom_row["pack_size_kg"]) if bom_row["pack_size_kg"] is not None else None
            bom_version    = bom_row["version"]
            bom_bar_line_process = bom_row["bar_line_process"]
            bom_bar_line_routed  = bom_row["bar_line_routed"]

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

    # Accounting summary (job_card_accounting_v2). The JC detail GET was
    # historically silent about this row — the Output & Accounting tab's
    # Summary Card relied on the post-save GET /accounting refetch to
    # populate it, so a page refresh after a save showed all zeros and the
    # operator read it as "accounting got lost".
    #
    # Migration 049 made the row per-(JC, batch). Surface BOTH:
    #   * accounting_per_batch: list of per-batch rows so the
    #     SummaryCard's per-batch collapsibles render the saved
    #     IS_BALANCED / percentages for each batch directly.
    #   * accounting: JC-wide roll-up (SUM of kg columns, AND of
    #     is_balanced, recomputed percentages). Backward-compat with
    #     clients that read the single field; also feeds the TOTAL
    #     header chip on the SummaryCard.
    #
    # Pre-049 environments don't have a batch_id column on the table;
    # UndefinedColumnError → fall back to the JC-level single row.
    def _accf(v):
        if v is None or v == "":
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    tolerance_pct_raw = None
    if h.get("bom_id") is not None:
        tolerance_pct_raw = await conn.fetchval(
            "SELECT allowed_balance_tolerance_pct FROM bom_header WHERE bom_id = $1",
            h["bom_id"],
        )
    # Canonical default kept in sync with jc_accounting_v2.BALANCE_TOLERANCE_PCT_DEF;
    # inlined here to avoid a circular import (jc_accounting_v2 already
    # depends on job_card_v2 for assert_not_locked).
    tolerance_pct_val = float(tolerance_pct_raw) if tolerance_pct_raw is not None else 0.001

    try:
        accounting_rows = await conn.fetch(
            "SELECT * FROM job_card_accounting_v2 WHERE job_card_id = $1 "
            "ORDER BY COALESCE(batch_id, 0)",
            job_card_id,
        )
    except UndefinedColumnError:
        _warn_batch_id_missing_once("job_card_accounting_v2")
        # Pre-049 schema — the table has no batch_id. Fall back to the
        # JC-level single-row read. The per-batch list collapses to a
        # one-entry list with batch_id NULL so the frontend's
        # accounting-per-batch code path still works (NULL batch_id is
        # treated as "the JC-wide row" by the SummaryCard).
        legacy_row = await conn.fetchrow(
            "SELECT * FROM job_card_accounting_v2 WHERE job_card_id = $1",
            job_card_id,
        )
        accounting_rows = [legacy_row] if legacy_row else []

    def _decorate(row_payload: dict) -> dict:
        """Add derived rejection_pct / offgrade_pct + tolerance to a serialized row."""
        out_qty = _accf(row_payload.get("output_qty"))
        if out_qty > 0:
            rejection_qty = _accf(row_payload.get("rejection_qty"))
            offgrade_qty  = _accf(row_payload.get("offgrade_total_qty"))
            row_payload["rejection_pct"] = round((rejection_qty / out_qty) * 100, 3)
            row_payload["offgrade_pct"]  = round((offgrade_qty  / out_qty) * 100, 3)
        else:
            row_payload["rejection_pct"] = None
            row_payload["offgrade_pct"]  = None
        row_payload["allowed_balance_tolerance_pct"] = tolerance_pct_val
        return row_payload

    accounting_per_batch = [_decorate(_serialize(r)) for r in accounting_rows]

    # JC-wide roll-up. SUM additive columns, AND is_balanced, recompute
    # percentages from aggregated kg values. None when no rows exist
    # (fresh JC; frontend treats that the same as the legacy null).
    if accounting_per_batch:
        agg_total_input    = sum(_accf(r.get("total_input_qty"))    for r in accounting_per_batch)
        agg_output_qty     = sum(_accf(r.get("output_qty"))         for r in accounting_per_batch)
        agg_output_units   = sum(_accf(r.get("output_qty_units"))   for r in accounting_per_batch)
        agg_carried_in     = sum(_accf(r.get("carried_in_qty"))     for r in accounting_per_batch)
        agg_dispatched_out = sum(_accf(r.get("dispatched_out_qty")) for r in accounting_per_batch)
        agg_process_loss   = sum(_accf(r.get("process_loss_qty"))   for r in accounting_per_batch)
        agg_extra_give     = sum(_accf(r.get("extra_give_away_qty"))for r in accounting_per_batch)
        agg_balance_mat    = sum(_accf(r.get("balance_material_qty"))for r in accounting_per_batch)
        agg_offgrade       = sum(_accf(r.get("offgrade_total_qty")) for r in accounting_per_batch)
        agg_rejection      = sum(_accf(r.get("rejection_qty"))      for r in accounting_per_batch)
        agg_wastage        = sum(_accf(r.get("wastage_qty"))        for r in accounting_per_batch)
        agg_control_sample = sum(_accf(r.get("control_sample_qty")) for r in accounting_per_batch)
        agg_total_accounted= sum(_accf(r.get("total_accounted_qty"))for r in accounting_per_batch)
        agg_balance_diff   = sum(_accf(r.get("balance_difference_qty")) for r in accounting_per_batch)
        # All batches must be balanced for the JC roll-up to read balanced.
        # A NULL is_balanced (legacy row before B4 was wired) is treated as
        # False to keep close-gate semantics conservative.
        agg_is_balanced = all(bool(r.get("is_balanced")) for r in accounting_per_batch)

        # Recompute percentages from aggregated kg buckets so the roll-up
        # is internally consistent (not just SUM of per-batch percentages,
        # which doesn't aggregate when each batch has a different output).
        effective_process_loss = agg_process_loss + agg_wastage
        if agg_output_qty > 0:
            roll_process_loss_pct   = round((effective_process_loss      / agg_output_qty) * 100, 3)
            roll_ega_loss_pct       = round((agg_extra_give               / agg_output_qty) * 100, 3)
            roll_invisible_loss_pct = round(roll_process_loss_pct + roll_ega_loss_pct, 3)
            roll_total_loss_pct     = round(
                ((effective_process_loss + agg_extra_give + agg_offgrade) / agg_output_qty) * 100, 3,
            )
            roll_rejection_pct      = round((agg_rejection / agg_output_qty) * 100, 3)
            roll_offgrade_pct       = round((agg_offgrade  / agg_output_qty) * 100, 3)
        else:
            roll_process_loss_pct = roll_ega_loss_pct = None
            roll_invisible_loss_pct = roll_total_loss_pct = None
            roll_rejection_pct = roll_offgrade_pct = None

        accounting_payload = {
            # Identity — accounting_id absent because the roll-up isn't a
            # single row. batch_id null so the frontend knows this is the
            # aggregate, not a single batch row.
            "job_card_id":            job_card_id,
            "batch_id":               None,
            # Aggregate kg buckets
            "total_input_qty":        agg_total_input,
            "output_qty":             agg_output_qty,
            "output_qty_units":       agg_output_units if agg_output_units > 0 else None,
            "carried_in_qty":         agg_carried_in,
            "dispatched_out_qty":     agg_dispatched_out,
            "process_loss_qty":       agg_process_loss,
            "extra_give_away_qty":    agg_extra_give,
            "balance_material_qty":   agg_balance_mat,
            "offgrade_total_qty":     agg_offgrade,
            "rejection_qty":          agg_rejection,
            "wastage_qty":            agg_wastage,
            "control_sample_qty":     agg_control_sample,
            "total_accounted_qty":    agg_total_accounted,
            "balance_difference_qty": agg_balance_diff,
            "is_balanced":            agg_is_balanced,
            # Recomputed percentages
            "process_loss_pct":       roll_process_loss_pct,
            "ega_loss_pct":           roll_ega_loss_pct,
            "invisible_loss_pct":     roll_invisible_loss_pct,
            "total_loss_pct":         roll_total_loss_pct,
            "rejection_pct":          roll_rejection_pct,
            "offgrade_pct":           roll_offgrade_pct,
            # UOM — carry forward from the first row (all batches share JC UOM)
            "input_uom":              accounting_per_batch[0].get("input_uom"),
            "output_uom":             accounting_per_batch[0].get("output_uom"),
            "output_kind":            accounting_per_batch[0].get("output_kind"),
            # Tolerance from BOM
            "allowed_balance_tolerance_pct": tolerance_pct_val,
        }
    else:
        accounting_payload = None

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
        # G4 bar-line override badge (migration 061): the richer routing string
        # and whether this FG's route was re-derived from it. NULL pre-061.
        "bar_line_process": bom_bar_line_process,
        "bar_line_routed":  bom_bar_line_routed,
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
    # section_5_output is the batch-UNAWARE roll-up legacy/v1 clients read
    # (and the web form's no-batch fallback). It used to be outputs[-1] — the
    # single most-recently-saved row — so on a multi-batch JC it showed only
    # the LAST batch's numbers instead of the JC total. Aggregate the LATEST
    # output per batch (outputs is ordered recorded_at ASC, so the last row
    # seen per batch_id is that batch's newest save), then SUM across batches.
    # We take latest-per-batch rather than summing raw rows because output
    # writes are append-only — summing every row would double-count re-saves.
    last_output = _serialize(outputs[-1]) if outputs else None
    if outputs:
        latest_by_batch: dict = {}
        for _o in outputs:
            _row = _serialize(_o)
            latest_by_batch[_row.get("batch_id")] = _row
        _latest = list(latest_by_batch.values())
        agg_fg_kg    = sum(_accf(r.get("output_qty_kg"))   for r in _latest)
        agg_rm       = sum(_accf(r.get("rm_consumed_kg"))  for r in _latest)
        agg_loss     = sum(_accf(r.get("process_loss_kg")) for r in _latest)
        _any_units   = any(r.get("output_qty_units") is not None for r in _latest)
        agg_fg_units = sum(_accf(r.get("output_qty_units")) for r in _latest) if _any_units else None
        agg_yield    = round((agg_fg_kg / agg_rm) * 100, 3) if agg_rm > 0 else None
    section_5_output = {
        "output_id":       last_output.get("output_id")  if last_output else None,
        "fg_actual_kg":    agg_fg_kg    if outputs else None,
        "fg_actual_units": int(round(agg_fg_units)) if (outputs and agg_fg_units is not None) else None,
        "rm_consumed_kg":  agg_rm       if outputs else None,
        "process_loss_kg": agg_loss     if outputs else None,
        # Free-text remark from the latest output row (batch-unaware — the
        # web form's per-batch read pulls the batch-scoped remark straight
        # off detail.outputs instead).
        "process_loss_remark": last_output.get("process_loss_remark") if last_output else None,
        "yield_pct":       agg_yield    if outputs else None,
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
        # R10/B4 — accounting summary so the Output tab's Summary Card
        # hydrates on every detail fetch instead of only after the
        # operator presses Save Output (which fires a separate GET
        # /accounting refetch). See the fetch + post-process block above.
        #
        # Migration 049 made the underlying table per-(JC, batch).
        # `accounting` is the JC-wide roll-up (sum of kg, AND of
        # is_balanced) for backward compat with clients that read the
        # single field. `accounting_per_batch` is the array of per-batch
        # rows the SummaryCard's per-batch collapsibles render directly.
        "accounting":                     accounting_payload,
        "accounting_per_batch":           accounting_per_batch,
        "store_allocations":              [],          # see /allocations endpoint (TODO)
        # total_stages for the chain progress bar.
        "total_stages": await conn.fetchval(
            "SELECT COUNT(*) FROM job_card_v2 WHERE plan_line_id=$1 AND deleted_at IS NULL",
            h.get("plan_line_id"),
        ) if h.get("plan_line_id") else None,
    }
