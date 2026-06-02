"""R13 phased closure - per-day phases inside a v2 JC.

Each JC can host multiple phases (one per calendar day by convention).
A phase opens, accumulates work (shift logs, outputs), and closes with
that day's produced qty. The close transaction also auto-dispatches the
produced qty to the next stage's JC so downstream can start their phase.

DB invariants relied on (migration 029):
  * UNIQUE (job_card_id, phase_number)  - phase numbers monotonic per JC
  * UNIQUE (job_card_id, phase_date)    - one phase per calendar day
  * Partial UNIQUE uq_jcphase_one_open  - at most one open phase per JC

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

from app.core.helpers import insert_with_pk_retry, new_short_time_id
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
# Open phase
# ---------------------------------------------------------------------------

async def open_phase(conn, *, job_card_id: int,
                     planned_qty_kg: float | None = None,
                     phase_date: date | None = None,
                     notes: str | None = None) -> dict:
    """Allocate a new phase row. phase_number = max + 1; phase_date defaults
    to today. Returns {"opened": True, "phase": <row>} or an error dict.

    Refuses (409) if another phase is already open for this JC.

    B9 C1 fix: SELECT ... FOR UPDATE on the parent JC row serialises
    concurrent /phase/open calls so two operators cannot both compute the
    same next phase_number, and so the partial UNIQUE uq_jcphase_one_open
    never gets a chance to raise. We also catch non-PK UniqueViolation
    defensively for the rare residual race window.
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

    existing_open = await conn.fetchrow(
        "SELECT phase_id, phase_number FROM job_card_phase_v2 "
        "WHERE  job_card_id=$1 AND status='open'",
        job_card_id,
    )
    if existing_open:
        return {
            "error":        "phase_already_open",
            "open_phase_id": existing_open["phase_id"],
            "phase_number": existing_open["phase_number"],
            "message": (
                "Close (or cancel) the currently open phase before "
                "opening another one."
            ),
        }

    next_number = await conn.fetchval(
        "SELECT COALESCE(MAX(phase_number), 0) + 1 FROM job_card_phase_v2 "
        "WHERE  job_card_id=$1",
        job_card_id,
    )
    if phase_date is None:
        phase_date = await conn.fetchval("SELECT CURRENT_DATE")

    async def _insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_phase_v2 (
                phase_id, job_card_id, phase_number, phase_date,
                planned_qty_kg, status, notes
            ) VALUES ($1, $2, $3, $4, $5, 'open', $6)
            RETURNING *
            """,
            new_short_time_id(),
            job_card_id, next_number, phase_date,
            planned_qty_kg, notes,
        )
    try:
        row = await insert_with_pk_retry(conn, _insert)
    except UniqueViolationError as exc:
        constraint = getattr(exc, "constraint_name", "") or ""
        if "one_open" in constraint:
            return {"error": "phase_already_open",
                    "message": "A concurrent /phase/open beat us to it."}
        if "phase_date" in constraint:
            return {"error": "phase_date_taken",
                    "phase_date": phase_date.isoformat() if phase_date else None,
                    "message": f"A phase already exists for {phase_date}."}
        if "phase_number" in constraint:
            return {"error": "phase_number_taken",
                    "phase_number": next_number,
                    "message": "A concurrent /phase/open claimed this number."}
        raise
    return {"opened": True, "phase": _serialize(row)}


# ---------------------------------------------------------------------------
# Close phase (the heavy one - all side effects in one txn)
# ---------------------------------------------------------------------------

async def close_phase(conn, *, phase_id: int,
                      job_card_id: int | None = None,
                      produced_qty_kg: float,
                      output_kind: str | None = None,
                      output_uom: str | None = None,
                      output_qty_units: float | None = None,
                      yield_pct: float | None = None,
                      rm_consumed_kg: float = 0.0,
                      extra_give_away_qty: float = 0.0,
                      notes: str | None = None,
                      closed_by: str | None = None) -> dict:
    """Close a phase and propagate its produced qty downstream in one txn.

    Side effects:
      1. UPDATE job_card_phase_v2 -> status='closed', produced_qty_kg,
         extra_give_away_qty, closed_at, closed_by, ended_at.
      2. INSERT job_card_output_v2 tagged with phase_id (so the output
         row carries the phase context for daily reporting).
      3. If the parent JC has a next_job_card_id, INSERT a row in
         job_card_partial_dispatch_v2 (phase-tagged) and update both
         sides' running totals: this JC's dispatched_to_next_kg and the
         downstream JC's carried_qty_kg.
      4. If the downstream JC is locked with reason='awaiting_previous_stage',
         flip is_locked=FALSE, status='unlocked', locked_reason=NULL so
         the downstream floor can start work on what just arrived.

    B9 C2 fix: SELECT phase ... FOR UPDATE locks the phase row, so two
    concurrent close calls cannot both pass the status='open' check and
    double-write the dispatch / running totals.

    B9 H1: when `job_card_id` is supplied (by the router from the path),
    we assert it matches phase.job_card_id - prevents tenancy spoofing
    via cross-JC phase URLs.

    B9 H2: yield_pct is clamped to _YIELD_PCT_MAX so an operator typo
    (rm_consumed_kg≈0.001) cannot overflow NUMERIC(6,3).

    B9 H3: phase notes are APPENDED on close (not overwritten) so a
    note recorded at /phase/open survives /phase/close. The output row
    gets the close-specific notes verbatim.
    """
    # B9 C2: lock the phase row before any reads. _lock_check_via_phase
    # SELECTs job_card_id (no lock); we re-SELECT the phase here with
    # FOR UPDATE to serialise close attempts.
    lock_err_jc = await _lock_check_via_phase(conn, phase_id)
    if isinstance(lock_err_jc, dict):
        return lock_err_jc
    resolved_jc_id = lock_err_jc  # parent JC id resolved via the phase row

    # B9 H1: enforce path-vs-row consistency before doing any work.
    if job_card_id is not None and job_card_id != resolved_jc_id:
        return {
            "error": "phase_jc_mismatch",
            "path_job_card_id": job_card_id,
            "phase_job_card_id": resolved_jc_id,
            "message": (
                "The phase belongs to a different job card than the URL path. "
                "Use the correct {job_card_id}/{phase_id} pair."
            ),
        }

    if produced_qty_kg is None or produced_qty_kg < 0:
        return {
            "error": "invalid_produced_qty",
            "message": "produced_qty_kg must be >= 0",
        }

    phase = await conn.fetchrow(
        "SELECT phase_id, job_card_id, phase_number, status, notes "
        "FROM   job_card_phase_v2 WHERE phase_id=$1 "
        "FOR    UPDATE",
        phase_id,
    )
    if phase is None:
        return {"error": "phase_not_found"}
    if phase["status"] != 'open':
        return {
            "error":  "phase_not_open",
            "status": phase["status"],
            "message": f"Cannot close a phase in status '{phase['status']}'.",
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

    # ── 1. Close the phase row. Notes APPEND, not replace.
    updated_phase = await conn.fetchrow(
        """
        UPDATE job_card_phase_v2
           SET status              = 'closed',
               produced_qty_kg     = $2,
               extra_give_away_qty = $3,
               ended_at            = NOW(),
               closed_at           = NOW(),
               closed_by           = $4,
               notes               = CASE
                                       WHEN $5::text IS NULL THEN notes
                                       WHEN notes IS NULL    THEN $5
                                       ELSE notes || E'\n' || $5
                                     END
         WHERE phase_id = $1
        RETURNING *
        """,
        phase_id, produced_qty_kg, extra_give_away_qty, closed_by, notes,
    )

    # ── 2. Record the output row, tagged with phase_id. yield_pct was
    # validated above before any destructive write.
    resolved_output_kind = output_kind or jc["output_kind"] or 'SFG'
    output_row = await conn.fetchrow(
        """
        INSERT INTO job_card_output_v2 (
            job_card_id, phase_id, rm_consumed_kg, output_qty_kg,
            output_qty_units, output_kind, uom, yield_pct,
            notes, recorded_by
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING *
        """,
        resolved_jc_id, phase_id, rm_consumed_kg, produced_qty_kg,
        output_qty_units, resolved_output_kind,
        output_uom or jc["uom"],
        yield_pct, notes, closed_by,
    )

    # ── 3. Dispatch downstream (only when there's a next JC and qty > 0).
    dispatch_row = None
    downstream_unlocked = False
    if jc["next_job_card_id"] is not None and produced_qty_kg > 0:
        dispatch_row = await conn.fetchrow(
            """
            INSERT INTO job_card_partial_dispatch_v2 (
                from_job_card_id, to_job_card_id, qty_kg, qty_units,
                dispatched_by, phase_id,
                notes
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            resolved_jc_id, jc["next_job_card_id"], produced_qty_kg,
            output_qty_units, closed_by, phase_id,
            f"Auto-dispatch on phase {phase['phase_number']} close",
        )
        await conn.execute(
            "UPDATE job_card_v2 SET dispatched_to_next_kg = "
            "dispatched_to_next_kg + $2 WHERE job_card_id=$1",
            resolved_jc_id, produced_qty_kg,
        )
        await conn.execute(
            "UPDATE job_card_v2 SET carried_qty_kg = "
            "carried_qty_kg + $2 WHERE job_card_id=$1",
            jc["next_job_card_id"], produced_qty_kg,
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
        "phase":               _serialize(updated_phase),
        "output":              _serialize(output_row),
        "dispatch":            _serialize(dispatch_row) if dispatch_row else None,
        "downstream_unlocked": downstream_unlocked,
    }


async def _lock_check_via_phase(conn, phase_id: int):
    """Resolve phase_id -> parent job_card_id and run assert_not_locked.
    Returns the job_card_id (int) on success or the lock-error dict on
    refusal. Returning the JC id avoids a second SELECT in close_phase /
    cancel_phase paths.
    """
    jc_id = await conn.fetchval(
        "SELECT job_card_id FROM job_card_phase_v2 WHERE phase_id=$1",
        phase_id,
    )
    if jc_id is None:
        return {"error": "phase_not_found"}
    lock_err = await assert_not_locked(conn, jc_id)
    if lock_err:
        return lock_err
    return jc_id


# ---------------------------------------------------------------------------
# List phases
# ---------------------------------------------------------------------------

async def list_phases(conn, job_card_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT * FROM job_card_phase_v2
        WHERE  job_card_id=$1
        ORDER  BY phase_number
        """,
        job_card_id,
    )
    return [_serialize(r) for r in rows]


# ---------------------------------------------------------------------------
# Cancel phase
# ---------------------------------------------------------------------------

async def cancel_phase(conn, *, phase_id: int,
                       job_card_id: int | None = None,
                       reason: str | None = None,
                       cancelled_by: str | None = None) -> dict:
    """Mark a phase 'cancelled'. Refused when output / dispatch / shift
    rows already reference it - cancellation only makes sense for an
    empty phase (e.g., one opened by mistake).

    B9 H1 + M3 fix:
      * Path-vs-row tenancy check when job_card_id is supplied.
      * closed_at / closed_by left NULL on a cancelled row (those
        columns are reserved for `status='closed'` only). ended_at
        carries the cancel timestamp - the chk_jcphase_ended_at_when_
        terminal constraint requires that. Daily-summary queries
        filtering `WHERE closed_at IS NOT NULL` will correctly skip
        cancellations.
    """
    lock_err_jc = await _lock_check_via_phase(conn, phase_id)
    if isinstance(lock_err_jc, dict):
        return lock_err_jc
    resolved_jc_id = lock_err_jc

    if job_card_id is not None and job_card_id != resolved_jc_id:
        return {
            "error": "phase_jc_mismatch",
            "path_job_card_id": job_card_id,
            "phase_job_card_id": resolved_jc_id,
            "message": (
                "The phase belongs to a different job card than the URL path."
            ),
        }

    phase = await conn.fetchrow(
        "SELECT phase_id, status FROM job_card_phase_v2 WHERE phase_id=$1 "
        "FOR    UPDATE",
        phase_id,
    )
    if phase is None:
        return {"error": "phase_not_found"}
    if phase["status"] != 'open':
        return {
            "error": "phase_not_open",
            "status": phase["status"],
            "message": "Only open phases can be cancelled.",
        }

    # B9 M1: independent EXISTS clauses so each short-circuits on first
    # match rather than running all three branches.
    attached = await conn.fetchval(
        """
        SELECT EXISTS (SELECT 1 FROM job_card_output_v2          WHERE phase_id=$1)
            OR EXISTS (SELECT 1 FROM job_card_partial_dispatch_v2 WHERE phase_id=$1)
            OR EXISTS (SELECT 1 FROM job_card_shift_log_v2        WHERE phase_id=$1)
        """,
        phase_id,
    )
    if attached:
        return {
            "error": "phase_has_attached_rows",
            "message": (
                "Phase has output / dispatch / shift rows attached - "
                "cancel is only allowed on empty phases."
            ),
        }

    row = await conn.fetchrow(
        """
        UPDATE job_card_phase_v2
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
         WHERE phase_id = $1
        RETURNING *
        """,
        phase_id, reason,
    )
    return {"cancelled": True, "phase": _serialize(row)}
