"""Backfill synthesized phases for migration 029 (R13 phased closure).

For every existing JC without a phase row, insert ONE closed phase using
the canonical 8-digit short-time-ID generator (new_short_time_id +
insert_with_pk_retry from app.core.helpers). Then stamp existing rows
in job_card_output_v2, job_card_partial_dispatch_v2, and
job_card_shift_log_v2 with the synthesized phase_id.

Run from the backend root:

    PYTHONPATH=. uv run python scripts/029_backfill_phases.py

Idempotent. Re-running is a no-op once every JC has a phase row.
The whole backfill is wrapped in a single transaction so a partial
failure rolls back cleanly.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Self-bootstrap: scripts/ sits next to app/. Adding the backend root to
# sys.path lets `python scripts/029_backfill_phases.py` resolve the `app`
# package regardless of PYTHONPATH or whether the runner is uv / venv.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.core.helpers import insert_with_pk_retry, new_short_time_id  # noqa: E402
from app.db.connection import close_pool, create_pool  # noqa: E402


async def _insert_phase_for_jc(conn, jc) -> int:
    """Insert ONE synthesized closed phase for a JC. Returns the phase_id."""
    async def _do_insert():
        return await conn.fetchrow(
            """
            INSERT INTO job_card_phase_v2 (
                phase_id, job_card_id, phase_number, phase_date,
                planned_qty_kg, produced_qty_kg,
                started_at, ended_at, status,
                closed_by, closed_at, notes
            ) VALUES (
                $1, $2, 1, $3,
                $4, $5,
                $6, $7, 'closed',
                $8, $9, $10
            )
            RETURNING phase_id
            """,
            new_short_time_id(),
            jc["job_card_id"],
            jc["phase_date"],
            jc["planned_qty_kg"],
            jc["produced_qty_kg"],
            jc["started_at"],
            jc["ended_at"],
            "migration_029_backfill",
            jc["closed_at"],
            "Synthesized by scripts/029_backfill_phases.py for phased-closure FK backfill.",
        )

    row = await insert_with_pk_retry(conn, _do_insert)
    return row["phase_id"]


async def main():
    settings = Settings()
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. JCs that still need a phase row.
                #    produced_qty_kg snapshot = sum of this JC's outputs.
                pending = await conn.fetch(
                    """
                    SELECT
                        jc.job_card_id,
                        COALESCE(jc.start_time::date, jc.created_at::date) AS phase_date,
                        jc.planned_qty_kg,
                        (SELECT COALESCE(SUM(o.output_qty_kg), 0)
                         FROM   job_card_output_v2 o
                         WHERE  o.job_card_id = jc.job_card_id)             AS produced_qty_kg,
                        COALESCE(jc.start_time, jc.created_at)              AS started_at,
                        jc.end_time                                         AS ended_at,
                        COALESCE(jc.end_time, jc.created_at)                AS closed_at
                    FROM   job_card_v2 jc
                    WHERE  NOT EXISTS (
                        SELECT 1 FROM job_card_phase_v2 jp
                        WHERE  jp.job_card_id = jc.job_card_id
                    )
                    """
                )

                if not pending:
                    print("No JCs pending backfill - every JC already has a phase row.")
                else:
                    print(f"Backfilling phases for {len(pending)} JC(s)...")
                    for i, jc in enumerate(pending, start=1):
                        phase_id = await _insert_phase_for_jc(conn, jc)
                        if i % 100 == 0 or i == len(pending):
                            print(f"  [{i}/{len(pending)}] jc={jc['job_card_id']} -> phase={phase_id}")

                # 2. Stamp existing child rows with phase_id (idempotent).
                #    All three UPDATEs run regardless of step 1's count
                #    so re-running this script after a partial-failure
                #    earlier run will still finish the stamping.
                #    Pinned to the migration_029_backfill sentinel (H2 fix
                #    from the Phase A review) so a re-run after the R13
                #    service is live cannot accidentally stamp child rows
                #    with a real production phase.
                out_count = await conn.execute(
                    """
                    UPDATE job_card_output_v2 o
                    SET    phase_id = jp.phase_id
                    FROM   job_card_phase_v2 jp
                    WHERE  jp.job_card_id = o.job_card_id
                      AND  o.phase_id IS NULL
                      AND  jp.closed_by = 'migration_029_backfill'
                    """
                )
                dsp_count = await conn.execute(
                    """
                    UPDATE job_card_partial_dispatch_v2 d
                    SET    phase_id = jp.phase_id
                    FROM   job_card_phase_v2 jp
                    WHERE  jp.job_card_id = d.from_job_card_id
                      AND  d.phase_id IS NULL
                      AND  jp.closed_by = 'migration_029_backfill'
                    """
                )
                shf_count = await conn.execute(
                    """
                    UPDATE job_card_shift_log_v2 s
                    SET    phase_id = jp.phase_id
                    FROM   job_card_phase_v2 jp
                    WHERE  jp.job_card_id = s.job_card_id
                      AND  s.phase_id IS NULL
                      AND  jp.closed_by = 'migration_029_backfill'
                    """
                )
                print(f"\nChild rows stamped:")
                print(f"  job_card_output_v2:           {out_count}")
                print(f"  job_card_partial_dispatch_v2: {dsp_count}")
                print(f"  job_card_shift_log_v2:        {shf_count}")

                # 3. Final verification - print the same counts the SQL
                #    verification block would produce.
                summary = await conn.fetch(
                    """
                    SELECT 'jc_count'                  AS metric, COUNT(*)::TEXT AS value FROM job_card_v2
                    UNION ALL
                    SELECT 'phase_row_count'           , COUNT(*)::TEXT FROM job_card_phase_v2
                    UNION ALL
                    SELECT 'output_rows_with_phase'    ,
                           (COUNT(*) FILTER (WHERE phase_id IS NOT NULL))::TEXT || ' / ' || COUNT(*)::TEXT
                    FROM   job_card_output_v2
                    UNION ALL
                    SELECT 'dispatch_rows_with_phase'  ,
                           (COUNT(*) FILTER (WHERE phase_id IS NOT NULL))::TEXT || ' / ' || COUNT(*)::TEXT
                    FROM   job_card_partial_dispatch_v2
                    UNION ALL
                    SELECT 'shift_rows_with_phase'     ,
                           (COUNT(*) FILTER (WHERE phase_id IS NOT NULL))::TEXT || ' / ' || COUNT(*)::TEXT
                    FROM   job_card_shift_log_v2
                    """
                )
                print("\nVerification:")
                for row in summary:
                    print(f"  {row['metric']:<28} = {row['value']}")
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
