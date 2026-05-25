"""Inspect prod_order_number sequence — why does MAX+1 collide with PRD-2026-0036?

Hypothesis: there's a record with prod_order_number whose suffix doesn't
parse cleanly as INT, so MAX skips it, but the unique constraint still
catches the collision when we generate the same number.
"""
import asyncio
import asyncpg

from app.config import Settings

settings = Settings()


async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            # 1. All PRD-2026-% records, raw
            rows = await conn.fetch(
                """
                SELECT prod_order_id, prod_order_number, plan_line_id, created_at
                FROM production_order
                WHERE prod_order_number LIKE 'PRD-2026-%'
                ORDER BY prod_order_number
                """,
            )
            print(f"Found {len(rows)} PRD-2026-% records:")
            for r in rows:
                print(f"  id={r['prod_order_id']:4} num={r['prod_order_number']!r:25} "
                      f"plan_line={r['plan_line_id']} created={r['created_at']}")

            # 2. What does MAX+1 currently return?
            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(CAST(SUBSTRING(prod_order_number FROM 10) AS INT)), 0) + 1 "
                "FROM production_order WHERE prod_order_number LIKE $1",
                "PRD-2026-%",
            )
            print(f"\nMAX+1 returns: {seq}")
            print(f"Would generate: PRD-2026-{seq:04d}")

            # 3. Check for records where suffix doesn't cleanly cast to INT
            print("\n--- Suffix analysis ---")
            for r in rows:
                num = r['prod_order_number']
                suffix = num[9:] if len(num) >= 10 else ""
                try:
                    parsed = int(suffix)
                    flag = "OK"
                except ValueError:
                    parsed = None
                    flag = "FAIL"
                print(f"  {num!r:25} suffix={suffix!r:8} parsed={parsed} [{flag}]")

            # 4. Check the specific colliding record
            collide = await conn.fetchrow(
                "SELECT * FROM production_order WHERE prod_order_number = 'PRD-2026-0036'",
            )
            print(f"\nPRD-2026-0036 record exists: {bool(collide)}")
            if collide:
                print(f"  id={collide['prod_order_id']} plan_line_id={collide['plan_line_id']} "
                      f"status={collide['status']} created={collide['created_at']}")

            # 5. Check soft-delete / status columns that might filter MAX scan in code
            cols = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'production_order' AND column_name IN
                  ('deleted_at', 'status', 'is_deleted')
                """,
            )
            print(f"\nRelevant columns: {[c['column_name'] for c in cols]}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
