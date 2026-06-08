"""Pure FG-output validation + yield rule (no I/O), so it is unit-testable
in isolation. Used by job_card_v2.record_output.
"""
from __future__ import annotations


def compute_output_row(output_qty_kg: float | None,
                       rm_consumed_kg: float | None) -> dict:
    """Validate FG-output inputs, normalize optional RM consumption, and
    compute yield.

    Returns either an error dict ({"error": ...}) or a normalized result
    {"rm_consumed_kg", "output_qty_kg", "yield_pct"}.

    `rm_consumed_kg` is OPTIONAL. A packaging / later stage commonly records
    FG output (plus PM consumption) without an RM-consumption figure;
    requiring it rejected those saves with a 400. job_card_output_v2.
    rm_consumed_kg is NOT NULL DEFAULT 0, so a missing value normalizes to 0
    and yield is left uncomputed (NULL) — the same rule job_card_engine and
    job_card_batch_v2 already apply.

    `output_qty_kg` is the one genuinely required field (it is the quantity
    being recorded, and the column is NOT NULL CHECK >= 0).
    """
    if output_qty_kg is None:
        return {"error": "missing_qty"}
    rm = 0.0 if rm_consumed_kg is None else rm_consumed_kg
    if output_qty_kg < 0 or rm < 0:
        return {"error": "negative_qty"}
    yield_pct = None
    if rm > 0:
        yield_pct = round((output_qty_kg / rm) * 100, 3)
        # yield_pct lives in a NUMERIC(6,3) column — max abs value 999.999.
        # A yield outside +/-1000% almost always means a unit typo (output in
        # grams instead of kg, RM and output swapped). Reject loudly with the
        # numbers so the operator can see what tripped, rather than letting
        # asyncpg raise an opaque 500.
        if abs(yield_pct) >= 1000:
            return {
                "error": "implausible_yield",
                "yield_pct": yield_pct,
                "rm_consumed_kg": rm,
                "output_qty_kg": output_qty_kg,
            }
    return {"rm_consumed_kg": rm, "output_qty_kg": output_qty_kg, "yield_pct": yield_pct}
