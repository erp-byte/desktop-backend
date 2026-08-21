"""Repair accounting rows poisoned by the dispatched_out double-count.

Until the fix in jc_accounting_v2.save_accounting, the R9 conservation
identity added `dispatched_out_qty` to the OUT side alongside `output_qty`.
Those are not disjoint: `output_qty` is the batch's FULL production, and
`job_card_v2.dispatched_to_next_kg` (which `dispatched_out_qty` mirrors) is
incremented by close_batch's auto-dispatch with that same full produced qty.
Every dispatching stage therefore stored

    total_accounted_qty    = production counted twice
    balance_difference_qty = -dispatched_out_qty
    is_balanced            = false

and could not pass the /complete close gate (nor a second batch close),
even though the Output & Accounting tab — which never counted dispatch —
showed a green "Balanced / 0.00 kg".

The code fix only affects FUTURE saves. This script recomputes the three
derived columns on rows already written with the old equation, using the
same formula save_accounting now uses:

    total_accounted = output + process_loss + extra_give + control_sample
                    + rejection + offgrade + balance_material + wastage
                    + Σ return_qty (input_kind <> 'PM')
    diff            = total_input - total_accounted
    is_balanced     = |diff| / total_input <= bom tolerance   (input > 0)
                      |diff| <= 0.05 kg                       (input == 0)

Scope: ONLY rows with dispatched_out_qty > 0 — the rows this bug could
have touched. Rows with no dispatch are left strictly alone. Operator-entered
figures (output_qty, losses, off-grade, …) are never modified, and neither
are saved_by / saved_at — this rewrites derived columns only.

Genuine variances stay unbalanced: the script re-derives the verdict, it does
not force it to true.

Run from the backend root — DRY RUN (no writes) by default:

    PYTHONPATH=. uv run python scripts/repair_dispatch_double_count.py

Apply for real (single transaction, rolls back on any error):

    PYTHONPATH=. uv run python scripts/repair_dispatch_double_count.py --apply

Idempotent: once repaired, a re-run reports 0 changes.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Self-bootstrap: scripts/ sits next to app/.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.db.connection import close_pool, create_pool  # noqa: E402
from app.modules.production.services.jc_accounting_v2 import (  # noqa: E402
    BALANCE_TOLERANCE_PCT_DEF,
    BALANCE_TOLERANCE_QTY,
)

# Columns summed on the OUT side. Mirrors save_accounting — keep in step.
_OUT_COLS = (
    "output_qty", "process_loss_qty", "extra_give_away_qty", "control_sample_qty",
    "rejection_qty", "offgrade_total_qty", "balance_material_qty", "wastage_qty",
)


class _Rollback(Exception):
    """Aborts the transaction so a dry run can never leave writes behind."""


def _f(v) -> float:
    return float(v) if v is not None else 0.0


async def main() -> None:
    apply = "--apply" in sys.argv

    settings = Settings()
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    f"""
                    SELECT a.accounting_id, a.job_card_id, a.batch_id,
                           j.job_card_number, j.process_name, j.status AS jc_status,
                           a.total_input_qty, a.dispatched_out_qty,
                           a.total_accounted_qty, a.balance_difference_qty, a.is_balanced,
                           {', '.join('a.' + c for c in _OUT_COLS)},
                           COALESCE(b.allowed_balance_tolerance_pct, $1) AS tolerance_pct,
                           (SELECT COALESCE(SUM(m.return_qty), 0)
                              FROM job_card_material_consumption_v2 m
                             WHERE m.job_card_id = a.job_card_id
                               AND m.input_kind IS DISTINCT FROM 'PM') AS total_return
                      FROM job_card_accounting_v2 a
                      JOIN job_card_v2 j ON j.job_card_id = a.job_card_id
                 LEFT JOIN bom_header  b ON b.bom_id      = j.bom_id
                     WHERE a.dispatched_out_qty > 0
                       AND j.deleted_at IS NULL
                     ORDER BY a.job_card_id, COALESCE(a.batch_id, 0)
                    """,
                    BALANCE_TOLERANCE_PCT_DEF,
                )

                changed, unblocked, still_off = [], set(), []
                for r in rows:
                    total_input = _f(r["total_input_qty"])
                    new_accounted = round(
                        sum(_f(r[c]) for c in _OUT_COLS) + _f(r["total_return"]), 3
                    )
                    new_diff = round(total_input - new_accounted, 3)
                    tol = _f(r["tolerance_pct"]) or BALANCE_TOLERANCE_PCT_DEF
                    new_balanced = (
                        (abs(new_diff) / total_input) <= tol if total_input > 0
                        else abs(new_diff) <= BALANCE_TOLERANCE_QTY
                    )

                    if (new_balanced == r["is_balanced"]
                            and abs(new_accounted - _f(r["total_accounted_qty"])) < 0.0005
                            and abs(new_diff - _f(r["balance_difference_qty"])) < 0.0005):
                        continue  # already correct — idempotent re-run

                    changed.append((r, new_accounted, new_diff, new_balanced))
                    if new_balanced and r["is_balanced"] is not True:
                        unblocked.add(r["job_card_id"])
                    if not new_balanced:
                        still_off.append((r, new_diff))

                    if apply:
                        await conn.execute(
                            """
                            UPDATE job_card_accounting_v2
                               SET total_accounted_qty    = $2,
                                   balance_difference_qty = $3,
                                   is_balanced            = $4
                             WHERE accounting_id = $1
                            """,
                            r["accounting_id"], new_accounted, new_diff, new_balanced,
                        )

                mode = "APPLIED" if apply else "DRY RUN (no writes)"
                print(f"=== repair_dispatch_double_count — {mode} ===")
                print(f"rows with dispatched_out_qty > 0 : {len(rows)}")
                print(f"rows needing repair              : {len(changed)}")
                print(f"job cards unblocked for close    : {len(unblocked)}")
                print(f"rows still unbalanced after fix  : {len(still_off)} "
                      f"(genuine variances — gate correctly still refuses)")

                if changed:
                    print("\n--- repaired rows ---")
                    for r, acc, diff, bal in changed:
                        print(
                            f"  JC {r['job_card_id']} b={r['batch_id']} "
                            f"{(r['process_name'] or '')[:22]:<22} "
                            f"accounted {_f(r['total_accounted_qty']):>10.3f} -> {acc:>10.3f}   "
                            f"diff {_f(r['balance_difference_qty']):>10.3f} -> {diff:>9.3f}   "
                            f"balanced {r['is_balanced']} -> {bal}"
                        )

                if still_off:
                    print("\n--- these remain unbalanced (real variance to investigate) ---")
                    for r, diff in still_off:
                        print(f"  JC {r['job_card_id']} {r['job_card_number']} "
                              f"{(r['process_name'] or '')[:22]:<22} off by {abs(diff):.3f} kg")

                if not apply:
                    print("\nNo writes made. Re-run with --apply to commit.")
                    raise _Rollback()
    except _Rollback:
        pass
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
