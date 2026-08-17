"""READ-ONLY: find plans that a rejected POST /plans-v2 left behind.

Until the fix in router.create_plan_v2, a plan-create that failed BOM
resolution still committed everything written before the failing line — the
plan header, any lines that resolved first, and their so_fulfillment_v2
planned_qty bump — while the operator only saw a 400. Those plans generate no
job cards (or too few), so the floor never receives them.

This script writes nothing. It lists:
  1. draft plans with 0 lines               → safe to cancel/delete
  2. plans whose lines have no steps        → same failure, one line in
  3. fg_sku_names on recent plan lines that have no active bom_header row
     → the master data that has to be fixed before re-creating the plan

Run from the backend root, against whichever DB serves the environment:
    PYTHONPATH=. python scripts/find_orphan_plans.py
    DATABASE_URL=postgresql://... PYTHONPATH=. python scripts/find_orphan_plans.py
"""
from __future__ import annotations

import asyncio

from app.config import Settings
from app.db.connection import close_pool, create_pool

DAYS = 30


async def main() -> None:
    pool = await create_pool(Settings())
    try:
        async with pool.acquire() as conn:
            print(f"\n== draft plans with 0 lines (last {DAYS} days) ==")
            rows = await conn.fetch(
                """
                SELECT p.plan_id, p.status, p.entity, p.warehouse,
                       p.created_by, p.created_at::text AS created_at
                  FROM production_plan_v2 p
                 WHERE p.created_at > NOW() - ($1 || ' days')::interval
                   AND NOT EXISTS (SELECT 1 FROM production_plan_line_v2 l
                                    WHERE l.plan_id = p.plan_id)
                 ORDER BY p.created_at DESC
                """,
                str(DAYS),
            )
            print(f"   {len(rows)} found")
            for r in rows:
                print(f"   #{r['plan_id']}  {r['status']:<9} {r['entity']}/{r['warehouse']}"
                      f"  by {r['created_by']}  {r['created_at']}")

            print(f"\n== plan lines with no steps (last {DAYS} days) ==")
            rows = await conn.fetch(
                """
                SELECT l.plan_id, l.plan_line_id, l.fg_sku_name,
                       p.status, p.created_at::text AS created_at
                  FROM production_plan_line_v2 l
                  JOIN production_plan_v2 p ON p.plan_id = l.plan_id
                 WHERE p.created_at > NOW() - ($1 || ' days')::interval
                   AND NOT EXISTS (SELECT 1 FROM production_plan_step_v2 s
                                    WHERE s.plan_line_id = l.plan_line_id)
                 ORDER BY p.created_at DESC
                """,
                str(DAYS),
            )
            print(f"   {len(rows)} found")
            for r in rows:
                print(f"   #{r['plan_id']} line {r['plan_line_id']}  {r['status']:<9}"
                      f"  {r['fg_sku_name']}")

            print("\n== SKUs on recent plan lines with NO active BOM ==")
            rows = await conn.fetch(
                """
                SELECT DISTINCT l.fg_sku_name
                  FROM production_plan_line_v2 l
                  JOIN production_plan_v2 p ON p.plan_id = l.plan_id
                 WHERE p.created_at > NOW() - ($1 || ' days')::interval
                   AND NOT EXISTS (
                         SELECT 1 FROM bom_header b
                          WHERE b.fg_sku_name ILIKE l.fg_sku_name
                            AND b.is_active = TRUE)
                 ORDER BY 1
                """,
                str(DAYS),
            )
            print(f"   {len(rows)} found - create the BOM before re-planning these")
            for r in rows:
                print(f"   {r['fg_sku_name']}")
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    asyncio.run(main())
