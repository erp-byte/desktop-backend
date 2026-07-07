"""Diagnose + create JCs for any approved plan that has none.

Run from the backend root:
    PYTHONPATH=. uv run python scripts/fix_todays_plans.py
"""
from __future__ import annotations
import asyncio
from app.config import Settings
from app.db.connection import create_pool, close_pool
from app.modules.production.services.job_card_v2 import create_job_cards_from_plan


async def main():
    settings = Settings()
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as conn:
            # 1. Today's plans + their JC + line + step counts
            print("\n-- Today's plans --")
            rows = await conn.fetch("""
                SELECT p.plan_id, p.status,
                       p.created_at::text  AS created_at,
                       p.approved_at::text AS approved_at,
                       (SELECT COUNT(*) FROM production_plan_line_v2 pl
                          WHERE pl.plan_id = p.plan_id) AS line_count,
                       (SELECT COUNT(*) FROM production_plan_step_v2 ps
                          JOIN production_plan_line_v2 pl ON pl.plan_line_id = ps.plan_line_id
                          WHERE pl.plan_id = p.plan_id) AS step_count,
                       (SELECT COUNT(*) FROM job_card_v2 jc
                          WHERE jc.plan_id = p.plan_id AND jc.deleted_at IS NULL) AS jc_count
                FROM   production_plan_v2 p
                WHERE  p.created_at::date  = CURRENT_DATE
                   OR  p.approved_at::date = CURRENT_DATE
                ORDER  BY p.plan_id
            """)
            if not rows:
                print("  (none — no plans created/approved today)")
                return

            for r in rows:
                print(f"  plan_id={r['plan_id']:<12} status={r['status']:<10} "
                      f"lines={r['line_count']:<2} steps={r['step_count']:<2} "
                      f"jcs={r['jc_count']:<2} approved_at={r['approved_at']}")

            # 2. Plans that need JC creation
            approved_no_jcs = [r for r in rows
                              if r['status'] == 'approved' and r['jc_count'] == 0]
            if not approved_no_jcs:
                print("\nNothing to fix — every approved plan already has JCs.")
                return

            print(f"\n-- Creating JCs for {len(approved_no_jcs)} approved plan(s) --")
            for r in approved_no_jcs:
                if r['step_count'] == 0:
                    print(f"  plan_id={r['plan_id']}: SKIP — plan has no steps "
                          f"(add steps via Plan UI, then re-run)")
                    continue
                try:
                    async with conn.transaction():
                        result = await create_job_cards_from_plan(conn, r['plan_id'])
                    print(f"  plan_id={r['plan_id']}: {result}")
                except Exception as e:
                    print(f"  plan_id={r['plan_id']}: ERROR {type(e).__name__}: {e}")
    finally:
        await close_pool(pool)


asyncio.run(main())
