"""Repair plan lines whose stage order puts packing BEFORE a production stage.

Root cause: a {Packaging, Sorting} route was snapshotted into
production_plan_step_v2 and frozen into the job_card_v2 chain at generation
time, so the chain unlocks Packaging first and locks Sorting — the floor is
told to pack before it sorts (observed: "Solimo Premium Pine Nuts, 250g" in
plan 58795071). The create_plan guard (plan_v2.order_steps_packing_last) stops
NEW occurrences; this script repairs existing data.

SAFE-ONLY. A line is repaired only if EVERY job card on it is still in its
initial state:
    status IN ('locked','unlocked')  AND  start_time IS NULL
    AND no rows in job_card_output_v2 / job_card_material_consumption_v2
Lines where work has begun (assigned/started/has output/has consumption) are
REPORTED for manual handling and never auto-mutated — reordering a started
chain would unlock/relock cards mid-run.

Dry-run by default; pass --apply to commit.

    PYTHONPATH=. uv run python scripts/fix_inverted_step_order.py          # dry-run
    PYTHONPATH=. uv run python scripts/fix_inverted_step_order.py --apply  # commit
"""
from __future__ import annotations

import asyncio
import sys

from app.config import Settings
from app.db.connection import create_pool, close_pool
from app.modules.production.services.job_card_v2 import is_packing_stage
from app.modules.production.services.plan_v2 import order_steps_packing_last

_SAFE_STATUSES = ("locked", "unlocked")


def _stage_of(row):
    return row["stage"] or row["process_name"]


def _is_inverted(rows) -> bool:
    """True if a packing stage precedes any non-packing stage in `rows`."""
    seen_packing = False
    for r in rows:
        if is_packing_stage(_stage_of(r)):
            seen_packing = True
        elif seen_packing:
            return True
    return False


async def main(apply: bool) -> None:
    settings = Settings()
    pool = await create_pool(settings)
    would_fix = flagged = 0
    try:
        async with pool.acquire() as conn:
            lines = await conn.fetch(
                """
                SELECT pl.plan_line_id, pl.plan_id, pl.fg_sku_name,
                       p.status AS plan_status
                FROM   production_plan_line_v2 pl
                JOIN   production_plan_v2 p ON p.plan_id = pl.plan_id
                ORDER  BY pl.plan_id, pl.plan_line_id
                """
            )
            for ln in lines:
                pl_id = ln["plan_line_id"]
                steps = await conn.fetch(
                    """
                    SELECT step_id, step_order, process_name, stage
                    FROM   production_plan_step_v2
                    WHERE  plan_line_id = $1
                    ORDER  BY step_order
                    """,
                    pl_id,
                )
                if len(steps) < 2 or not _is_inverted(steps):
                    continue

                jcs = await conn.fetch(
                    """
                    SELECT job_card_id, step_number, process_name, stage,
                           status, start_time
                    FROM   job_card_v2
                    WHERE  plan_line_id = $1 AND deleted_at IS NULL
                    ORDER  BY step_number
                    """,
                    pl_id,
                )

                cur = [s["process_name"] for s in steps]
                want = [s["process_name"] for s in order_steps_packing_last(steps, stage_of=_stage_of)]
                print(f"plan_id={ln['plan_id']} line={pl_id} "
                      f"sku={ln['fg_sku_name']!r} plan_status={ln['plan_status']}")
                print(f"   steps now : {cur}")
                print(f"   steps want: {want}")

                # Safety: every JC must be untouched.
                started = [j for j in jcs
                           if j["status"] not in _SAFE_STATUSES or j["start_time"] is not None]
                has_activity = False
                if jcs:
                    jc_ids = [j["job_card_id"] for j in jcs]
                    out_n = await conn.fetchval(
                        "SELECT COUNT(*) FROM job_card_output_v2 WHERE job_card_id = ANY($1)", jc_ids)
                    con_n = await conn.fetchval(
                        "SELECT COUNT(*) FROM job_card_material_consumption_v2 WHERE job_card_id = ANY($1)", jc_ids)
                    has_activity = bool(out_n) or bool(con_n)

                if started or has_activity:
                    flagged += 1
                    print("   ⚠ work has begun (assigned/started/output/consumption) "
                          "— SKIPPED; fix manually")
                    continue

                if not apply:
                    would_fix += 1
                    print("   → would repair (dry-run)")
                    continue

                async with conn.transaction():
                    # 1. plan-step order
                    for new_order, s in enumerate(order_steps_packing_last(steps, stage_of=_stage_of), start=1):
                        await conn.execute(
                            "UPDATE production_plan_step_v2 SET step_order = $1 WHERE step_id = $2",
                            new_order, s["step_id"],
                        )
                    # 2. existing JC chain — same packing-last rule, then renumber + relink
                    ordered = order_steps_packing_last(jcs, stage_of=_stage_of)
                    n = len(ordered)
                    for idx, j in enumerate(ordered):
                        is_first = idx == 0
                        await conn.execute(
                            """
                            UPDATE job_card_v2
                            SET    step_number      = $1,
                                   is_locked        = $2,
                                   status           = $3,
                                   locked_reason    = $4,
                                   prev_job_card_id = $5,
                                   next_job_card_id = $6
                            WHERE  job_card_id = $7
                            """,
                            idx + 1,
                            (not is_first),
                            ("unlocked" if is_first else "locked"),
                            (None if is_first else "awaiting_previous_stage"),
                            (ordered[idx - 1]["job_card_id"] if idx > 0 else None),
                            (ordered[idx + 1]["job_card_id"] if idx < n - 1 else None),
                            j["job_card_id"],
                        )
                    would_fix += 1
                    print("   ✓ repaired")

            verb = "repaired" if apply else "would repair"
            print(f"\nDone. {verb}={would_fix}, flagged_for_manual={flagged}")
            if not apply and would_fix:
                print("Re-run with --apply to commit the repairs above.")
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
