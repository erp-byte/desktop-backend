"""Quick scan: for today's JCs, show how many RM + PM indent rows each has.

Stage 1 should have rm_count + pm_count > 0. Later stages should be 0
(by design — only stage 1 is materialised).
"""
from __future__ import annotations
import asyncio
from app.config import Settings
from app.db.connection import create_pool, close_pool


async def main():
    settings = Settings()
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT jc.job_card_id, jc.plan_id, jc.step_number, jc.status,
                       jc.bom_id,
                       (SELECT COUNT(*) FROM job_card_rm_indent_v2 ri
                          WHERE ri.job_card_id = jc.job_card_id) AS rm_count,
                       (SELECT COUNT(*) FROM job_card_pm_indent_v2 pi
                          WHERE pi.job_card_id = jc.job_card_id) AS pm_count
                FROM   job_card_v2 jc
                JOIN   production_plan_v2 p ON p.plan_id = jc.plan_id
                WHERE  p.approved_at::date = CURRENT_DATE
                  AND  jc.deleted_at IS NULL
                ORDER  BY jc.plan_id, jc.step_number
            """)
            print(f"\n-- Today's JCs ({len(rows)} rows) --")
            for r in rows:
                flag = ""
                if r['step_number'] == 1 and (r['rm_count'] + r['pm_count']) == 0:
                    flag = "  <-- STAGE 1 HAS NO INDENTS (needs backfill)"
                print(f"  jc={r['job_card_id']:<10} plan={r['plan_id']:<10} "
                      f"step={r['step_number']:<2} status={r['status']:<12} "
                      f"bom={str(r['bom_id'] or 'NULL'):<10} "
                      f"rm={r['rm_count']:<2} pm={r['pm_count']:<2}{flag}")
    finally:
        await close_pool(pool)


asyncio.run(main())
