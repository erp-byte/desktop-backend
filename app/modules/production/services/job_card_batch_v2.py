"""R13 phased closure - per-day batches inside a v2 JC.

Each JC can host multiple batches (one per calendar day by convention
in Stage 1; Stage 2 will relax the per-day cap and the "single open"
rule). A batch opens, accumulates work (shift logs, outputs), and
closes with that day's produced qty. The close transaction also auto-
dispatches the produced qty to the next stage's JC so downstream can
start their batch.

Stage 1 compat layer (migration 036):
  This module writes through `job_card_batch_v2`, which during the
  Stage 1 window is an updatable VIEW over the original
  `job_card_phase_v2` table.  All INSERT / UPDATE / DELETE / SELECT
  pass straight to the underlying table — including FOR UPDATE row
  locks — because the view is a simple column-rename (PG auto-detects
  it as updatable, no INSTEAD OF triggers needed).

  Child tables (output / partial_dispatch / shift_log) get a sibling
  `batch_id` column kept in lockstep with the legacy `phase_id` column
  by a BEFORE INSERT/UPDATE trigger.  Old code reading `phase_id`
  still sees the same value the new code wrote to `batch_id`, and
  vice-versa.  The compat layer is dropped in migration 037 once the
  new code has been live cleanly; the view becomes the real table at
  that point.  This module needs no changes at the cutover — it
  already speaks batch_*.

DB invariants relied on (migration 029, surfaced through the view):
  * UNIQUE (job_card_id, phase_number)  -> view exposes as batch_number
  * UNIQUE (job_card_id, phase_date)    -> view exposes as batch_date
  * Partial UNIQUE uq_jcphase_one_open  -> "at most one open batch per JC"

(All three constraints stay in place for Stage 1 — Stage 2 drops the
"one open" and "one per day" rules per the operator-approved design.)

CHECK constraints from migration 030:
  * chk_jcphase_closed_at_when_closed  - status='closed' must have closed_at
  * chk_jcphase_ended_at_when_terminal - status in ('closed','cancelled')
                                          must have ended_at

R6 lock guard: open / close / cancel operations are 'operational' from
R6's point of view and route through assert_not_locked. The list endpoint
is read-only and exempt.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal

from asyncpg.exceptions import UniqueViolationError

from app.core.helpers import insert_with_pk_retry, ist_now_text, new_short_time_id
from app.modules.production.services.job_card_v2 import assert_not_locked

logger = logging.getLogger(__name__)

# B9 H2 fix: clamp yield_pct so an operator-typo (rm_consumed_kg≈0,
# produced_qty_kg=1) cannot overflow the NUMERIC(6,3) column on
# job_card_output_v2.yield_pct (max storable value 999.999).
_YIELD_PCT_MAX = 999.999


def _serialize(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Open batch
# ---------------------------------------------------------------------------

async def open_batch(conn, *, job_card_id: int,
                     planned_qty_kg: float | None = None,
                     batch_date: date | None = None,
                     input_qty_kg: float | None = None,
                     notes: str | None = None) -> dict:
    """Allocate a new batch row. batch_number = max + 1; batch_date defaults
    to today. Returns {"opened": True, "batch": <row>} or an error dict.

    Stage 2: multi-open allowed (migration 038 dropped the partial
    UNIQUE).  Concurrent /batches/open calls on the same JC now race
    cleanly against batch_number monotonicity via the FOR UPDATE on
    the parent JC row.

    `input_qty_kg` is recorded for the operator's reference (how much
    of the JC pool this batch claims to consume).  Not gated server-
    side — typo / over-claim is detected at close time by the loss-
    percent strip in the UI rollup.

    Stamps `opened_at_ist` with the IST clock face at the moment of
    open so reports can show the floor's local time without timezone
    setup.
    """
    lock_err = await assert_not_locked(conn, job_card_id)
    if lock_err:
        return lock_err

    jc = await conn.fetchrow(
        "SELECT job_card_id FROM job_card_v2 "
        "WHERE  job_card_id=$1 AND deleted_at IS NULL "
        "FOR    UPDATE",
        job_card_id,
    )
    if jc is None:
        return {"error": "job_card_not_found"}

    next_number = await conn.fetchval(
        "SELECT COALESCE(MAX(batch_number), 0) + 1 FROM job_card_batch_v2 "
        "WHERE  job_card_id=$1",
        job_card_id,
    )
    if batch_date is None:
        batch_date = await conn.fetchval("SELECT CURRENT_DATE")

    opened_at_ist = ist_now_text()

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_batch_v2 (
                batch_id, job_card_id, batch_number, batch_date,
                planned_qty_kg, input_qty_kg, status, notes,
                opened_at_ist
            ) VALUES ($1, $2, $3, $4, $5, $6, 'open', $7, $8)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, next_number, batch_date,
            planned_qty_kg, input_qty_kg, notes,
            opened_at_ist,
        )
    try:
        row = await insert_with_pk_retry(conn, _insert)
    except UniqueViolationError as exc:
        # Stage 2 dropped uq_*_one_open and (jc, phase_date) UNIQUE,
        # so neither "already open" nor "same-day" violations are
        # expected anymore. The matcher stays defensive so an
        # unmigrated DB or a future re-introduced constraint surfaces
        # a useful message instead of a raw 500.
        constraint = getattr(exc, "constraint_name", "") or ""
        if "one_open" in constraint:
            return {"error": "batch_already_open",
                    "message": "Multi-open is enabled but a legacy "
                               "constraint refused the insert — apply "
                               "migration 038."}
        if "batch_date" in constraint or "phase_date" in constraint:
            return {"error": "batch_date_taken",
                    "batch_date": batch_date.isoformat() if batch_date else None,
                    "message": f"A batch already exists for {batch_date} "
                               "(unexpected after migration 038)."}
        if "batch_number" in constraint or "phase_number" in constraint:
            return {"error": "batch_number_taken",
                    "batch_number": next_number,
                    "message": "A concurrent /batches/open claimed this number."}
        raise
    return {"opened": True, "batch": _serialize(row)}


# ---------------------------------------------------------------------------
# Close batch (the heavy one - all side effects in one txn)
# ---------------------------------------------------------------------------

async def close_batch(conn, *, batch_id: int,
                      job_card_id: int | None = None,
                      produced_qty_kg: float,
                      output_kind: str | None = None,
                      output_uom: str | None = None,
                      output_qty_units: float | None = None,
                      yield_pct: float | None = None,
                      rm_consumed_kg: float = 0.0,
                      extra_give_away_qty: float = 0.0,
                      # Stage 2 per-batch summary fields (all optional —
                      # the UI fills them from the live accounting
                      # snapshot on close).
                      input_qty_kg: float | None = None,
                      process_loss_kg: float | None = None,
                      control_sample_kg: float | None = None,
                      is_balanced: bool | None = None,
                      balance_difference_qty: float | None = None,
                      closure_remarks: str | None = None,
                      # Stage 3 final: operator-controlled partial
                      # dispatch on close.  None → dispatch the full
                      # produced_qty_kg (legacy behaviour).  A number →
                      # only that much flows to the next JC; the rest
                      # stays at this JC's stage.
                      dispatch_qty_kg: float | None = None,
                      notes: str | None = None,
                      closed_by: str | None = None) -> dict:
    """Close a batch and propagate its produced qty downstream in one txn.

    Side effects:
      1. UPDATE job_card_batch_v2 -> status='closed', produced_qty_kg,
         extra_give_away_qty, closed_at, closed_by, ended_at.
      2. INSERT job_card_output_v2 tagged with batch_id (so the output
         row carries the batch context for daily reporting).
      3. If the parent JC has a next_job_card_id, INSERT a row in
         job_card_partial_dispatch_v2 (batch-tagged) and update both
         sides' running totals: this JC's dispatched_to_next_kg and the
         downstream JC's carried_qty_kg.
      4. If the downstream JC is locked with reason='awaiting_previous_stage',
         flip is_locked=FALSE, status='unlocked', locked_reason=NULL so
         the downstream floor can start work on what just arrived.

    B9 C2 fix: SELECT batch ... FOR UPDATE locks the batch row, so two
    concurrent close calls cannot both pass the status='open' check and
    double-write the dispatch / running totals.

    B9 H1: when `job_card_id` is supplied (by the router from the path),
    we assert it matches batch.job_card_id - prevents tenancy spoofing
    via cross-JC batch URLs.

    B9 H2: yield_pct is clamped to _YIELD_PCT_MAX so an operator typo
    (rm_consumed_kg≈0.001) cannot overflow NUMERIC(6,3).

    B9 H3: batch notes are APPENDED on close (not overwritten) so a
    note recorded at /batches/open survives /batches/close. The output row
    gets the close-specific notes verbatim.
    """
    # B9 C2: lock the batch row before any reads. _lock_check_via_batch
    # SELECTs job_card_id (no lock); we re-SELECT the batch here with
    # FOR UPDATE to serialise close attempts.
    lock_err_jc = await _lock_check_via_batch(conn, batch_id)
    if isinstance(lock_err_jc, dict):
        return lock_err_jc
    resolved_jc_id = lock_err_jc  # parent JC id resolved via the batch row

    # B9 H1: enforce path-vs-row consistency before doing any work.
    if job_card_id is not None and job_card_id != resolved_jc_id:
        return {
            "error": "batch_jc_mismatch",
            "path_job_card_id": job_card_id,
            "batch_job_card_id": resolved_jc_id,
            "message": (
                "The batch belongs to a different job card than the URL path. "
                "Use the correct {job_card_id}/{batch_id} pair."
            ),
        }

    if produced_qty_kg is None or produced_qty_kg < 0:
        return {
            "error": "invalid_produced_qty",
            "message": "produced_qty_kg must be >= 0",
        }

    batch = await conn.fetchrow(
        "SELECT batch_id, job_card_id, batch_number, status, notes "
        "FROM   job_card_batch_v2 WHERE batch_id=$1 "
        "FOR    UPDATE",
        batch_id,
    )
    if batch is None:
        return {"error": "batch_not_found"}
    if batch["status"] != 'open':
        return {
            "error":  "batch_not_open",
            "status": batch["status"],
            "message": f"Cannot close a batch in status '{batch['status']}'.",
        }

    jc = await conn.fetchrow(
        """
        SELECT job_card_id, next_job_card_id, output_kind, uom,
               dispatched_to_next_kg
        FROM   job_card_v2
        WHERE  job_card_id=$1 AND deleted_at IS NULL
        FOR    UPDATE
        """,
        resolved_jc_id,
    )
    if jc is None:
        return {"error": "job_card_not_found"}

    # B9 H2: validate yield BEFORE destructive writes so the txn doesn't
    # roll back mid-flight on a typo.
    if rm_consumed_kg and rm_consumed_kg > 0 and yield_pct is None:
        raw_yield = (produced_qty_kg / rm_consumed_kg) * 100
        if raw_yield > _YIELD_PCT_MAX:
            return {
                "error": "yield_unreasonable",
                "computed_yield_pct": raw_yield,
                "produced_qty_kg":    produced_qty_kg,
                "rm_consumed_kg":     rm_consumed_kg,
                "message": (
                    f"Computed yield {raw_yield:.2f}% exceeds the storage "
                    f"limit ({_YIELD_PCT_MAX}%). Double-check that "
                    "produced_qty_kg and rm_consumed_kg are in the same "
                    "UOM (kg, not grams)."
                ),
            }
        yield_pct = round(raw_yield, 3)

    # ── 1. Close the batch row. Notes APPEND, not replace.
    # Stage 2: also writes the per-batch accounting summary + IST
    # timestamp literals.  When the caller doesn't pass a summary
    # field, COALESCE leaves the existing column value alone (open
    # batches normally have NULL summary fields).
    closed_at_ist_text = ist_now_text()
    updated_batch = await conn.fetchrow(
        """
        UPDATE job_card_batch_v2
           SET status                 = 'closed',
               produced_qty_kg        = $2,
               extra_give_away_qty    = $3,
               ended_at               = NOW(),
               closed_at              = NOW(),
               closed_by              = $4,
               notes                  = CASE
                                          WHEN $5::text IS NULL THEN notes
                                          WHEN notes IS NULL    THEN $5
                                          ELSE notes || E'\n' || $5
                                        END,
               input_qty_kg           = COALESCE($6, input_qty_kg),
               fg_actual_kg           = COALESCE($7, fg_actual_kg),
               fg_actual_units        = COALESCE($8, fg_actual_units),
               process_loss_kg        = COALESCE($9, process_loss_kg),
               control_sample_kg      = COALESCE($10, control_sample_kg),
               is_balanced            = COALESCE($11, is_balanced),
               balance_difference_qty = COALESCE($12, balance_difference_qty),
               closure_remarks        = COALESCE($13, closure_remarks),
               closed_at_ist          = $14,
               ended_at_ist           = $14
         WHERE batch_id = $1
        RETURNING *
        """,
        batch_id,
        produced_qty_kg, extra_give_away_qty, closed_by, notes,
        input_qty_kg,
        produced_qty_kg,                # fg_actual_kg mirrors produced
        output_qty_units,               # fg_actual_units
        process_loss_kg,
        control_sample_kg,
        is_balanced,
        balance_difference_qty,
        closure_remarks,
        closed_at_ist_text,
    )

    # ── 2. Record the output row, tagged with batch_id. yield_pct was
    # validated above before any destructive write.
    # output_id is the 8-digit time-based BIGINT PK, supplied by the app
    # (column is NOT NULL with no DB-side default) and retried on
    # collision via insert_with_pk_retry — same pattern record_output()
    # uses in services/job_card_v2.py.
    resolved_output_kind = output_kind or jc["output_kind"] or 'SFG'

    async def _insert_output_row():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_output_v2 (
                output_id,
                job_card_id, batch_id, rm_consumed_kg, output_qty_kg,
                output_qty_units, output_kind, uom, yield_pct,
                notes, recorded_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            new_short_time_id(),
            resolved_jc_id, batch_id, rm_consumed_kg, produced_qty_kg,
            output_qty_units, resolved_output_kind,
            output_uom or jc["uom"],
            yield_pct, notes, closed_by,
        )
    output_row = await insert_with_pk_retry(conn, _insert_output_row)

    # ── 3. Dispatch downstream (only when there's a next JC and qty > 0).
    # Stage 3: partial dispatch.  When the caller passes
    # `dispatch_qty_kg`, clamp to [0, produced_qty_kg] and ship only
    # that.  The remainder stays at this JC's stage and can be
    # dispatched later via /dispatch-to-next or a subsequent batch's
    # close.  Legacy callers (None) keep the full-amount auto-dispatch.
    if dispatch_qty_kg is None:
        effective_dispatch = produced_qty_kg
    else:
        effective_dispatch = max(0.0, min(float(dispatch_qty_kg), produced_qty_kg))

    dispatch_row = None
    downstream_unlocked = False
    if jc["next_job_card_id"] is not None and effective_dispatch > 0:
        # Units mirror the qty ratio so downstream sees a coherent
        # pair (qty_kg, qty_units).  When the operator splits the
        # batch we proportionally split the units.
        if (output_qty_units is not None
                and produced_qty_kg
                and produced_qty_kg > 0):
            effective_dispatch_units = (
                float(output_qty_units) * (effective_dispatch / produced_qty_kg)
            )
        else:
            effective_dispatch_units = output_qty_units

        note_suffix = (
            f"Partial dispatch ({effective_dispatch:.3f} of "
            f"{produced_qty_kg:.3f} kg)"
            if dispatch_qty_kg is not None
               and effective_dispatch != produced_qty_kg
            else f"Auto-dispatch on batch {batch['batch_number']} close"
        )

        # Migration 019 dropped BIGSERIAL on dispatch_id — ids are now
        # app-supplied via new_short_time_id() with the same PK-retry
        # pattern as record_output / open_batch above. Without this
        # wrapper, the INSERT sends NULL into a NOT NULL column and 500s
        # with NotNullViolationError on dispatch_id.
        async def _insert_dispatch():
            return await conn.fetchrow(
                """
                INSERT INTO job_card_partial_dispatch_v2 (
                    dispatch_id,
                    from_job_card_id, to_job_card_id, qty_kg, qty_units,
                    dispatched_by, batch_id,
                    notes
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                new_short_time_id(),
                resolved_jc_id, jc["next_job_card_id"], effective_dispatch,
                effective_dispatch_units, closed_by, batch_id,
                note_suffix,
            )
        dispatch_row = await insert_with_pk_retry(conn, _insert_dispatch)
        await conn.execute(
            "UPDATE job_card_v2 SET dispatched_to_next_kg = "
            "dispatched_to_next_kg + $2 WHERE job_card_id=$1",
            resolved_jc_id, effective_dispatch,
        )
        await conn.execute(
            "UPDATE job_card_v2 SET carried_qty_kg = "
            "carried_qty_kg + $2 WHERE job_card_id=$1",
            jc["next_job_card_id"], effective_dispatch,
        )
        # Unlock the downstream JC if it was awaiting this material.
        unlock_result = await conn.execute(
            """
            UPDATE job_card_v2
               SET is_locked     = FALSE,
                   status        = 'unlocked',
                   locked_reason = NULL
             WHERE job_card_id   = $1
               AND is_locked     = TRUE
               AND locked_reason = 'awaiting_previous_stage'
            """,
            jc["next_job_card_id"],
        )
        # asyncpg returns 'UPDATE <count>' - we just need the boolean.
        downstream_unlocked = "UPDATE 0" not in (unlock_result or "")

    return {
        "closed":              True,
        "batch":               _serialize(updated_batch),
        "output":              _serialize(output_row),
        "dispatch":            _serialize(dispatch_row) if dispatch_row else None,
        "downstream_unlocked": downstream_unlocked,
    }


async def _lock_check_via_batch(conn, batch_id: int):
    """Resolve batch_id -> parent job_card_id and run assert_not_locked.
    Returns the job_card_id (int) on success or the lock-error dict on
    refusal. Returning the JC id avoids a second SELECT in close_batch /
    cancel_batch paths.
    """
    jc_id = await conn.fetchval(
        "SELECT job_card_id FROM job_card_batch_v2 WHERE batch_id=$1",
        batch_id,
    )
    if jc_id is None:
        return {"error": "batch_not_found"}
    lock_err = await assert_not_locked(conn, jc_id)
    if lock_err:
        return lock_err
    return jc_id


# ---------------------------------------------------------------------------
# List batches
# ---------------------------------------------------------------------------

async def list_batches(conn, job_card_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT * FROM job_card_batch_v2
        WHERE  job_card_id=$1
        ORDER  BY batch_number
        """,
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ---------------------------------------------------------------------------
# Cancel batch
# ---------------------------------------------------------------------------

async def cancel_batch(conn, *, batch_id: int,
                       job_card_id: int | None = None,
                       reason: str | None = None,
                       cancelled_by: str | None = None) -> dict:
    """Mark a batch 'cancelled'. Refused when output / dispatch / shift
    rows already reference it - cancellation only makes sense for an
    empty batch (e.g., one opened by mistake).

    B9 H1 + M3 fix:
      * Path-vs-row tenancy check when job_card_id is supplied.
      * closed_at / closed_by left NULL on a cancelled row (those
        columns are reserved for `status='closed'` only). ended_at
        carries the cancel timestamp - the chk_jcphase_ended_at_when_
        terminal constraint requires that. Daily-summary queries
        filtering `WHERE closed_at IS NOT NULL` will correctly skip
        cancellations.
    """
    lock_err_jc = await _lock_check_via_batch(conn, batch_id)
    if isinstance(lock_err_jc, dict):
        return lock_err_jc
    resolved_jc_id = lock_err_jc

    if job_card_id is not None and job_card_id != resolved_jc_id:
        return {
            "error": "batch_jc_mismatch",
            "path_job_card_id": job_card_id,
            "batch_job_card_id": resolved_jc_id,
            "message": (
                "The batch belongs to a different job card than the URL path."
            ),
        }

    batch = await conn.fetchrow(
        "SELECT batch_id, status FROM job_card_batch_v2 WHERE batch_id=$1 "
        "FOR    UPDATE",
        batch_id,
    )
    if batch is None:
        return {"error": "batch_not_found"}
    if batch["status"] != 'open':
        return {
            "error": "batch_not_open",
            "status": batch["status"],
            "message": "Only open batches can be cancelled.",
        }

    # B9 M1: independent EXISTS clauses so each short-circuits on first
    # match rather than running all three branches.
    attached = await conn.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM job_card_output_v2          WHERE batch_id=$1)
            OR EXISTS (SELECT 1 FROM job_card_partial_dispatch_v2 WHERE batch_id=$1)
            OR EXISTS (SELECT 1 FROM job_card_shift_log_v2        WHERE batch_id=$1)
        """,
        batch_id,
    )
    if attached:
        return {
            "error": "batch_has_attached_rows",
            "message": (
                "Batch has output / dispatch / shift rows attached - "
                "cancel is only allowed on empty batches."
            ),
        }

    row = await conn.fetchrow(
        """
        UPDATE job_card_batch_v2
           SET status    = 'cancelled',
               ended_at  = COALESCE(ended_at, NOW()),
               notes     = CASE
                             WHEN $2::text IS NULL THEN
                               CASE WHEN notes IS NULL THEN 'Cancelled.'
                                    ELSE notes || E'\nCancelled.' END
                             ELSE
                               CASE WHEN notes IS NULL THEN 'Cancelled: ' || $2
                                    ELSE notes || E'\nCancelled: ' || $2 END
                           END
         WHERE batch_id = $1
        RETURNING *
        """,
        batch_id, reason,
    )
    return {"cancelled": True, "batch": _serialize(row)}
