"""One-off normalization of auth_user.allowed_warehouses.

The column historically stored two formats for the same site (legacy hyphenated
'A-185'/'W-202' alongside the canonical 'A185'/'W202'). This maps the hyphenated
warehouse codes to canonical, dedupes the array, and leaves genuinely-hyphenated
cold codes (D-39, D-514) untouched. Idempotent + transactional.

Run:  python scripts/normalize_warehouse_scope.py
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        before_a = await conn.fetchval("SELECT COUNT(*) FROM auth_user WHERE 'A-185' = ANY(allowed_warehouses)")
        before_w = await conn.fetchval("SELECT COUNT(*) FROM auth_user WHERE 'W-202' = ANY(allowed_warehouses)")
        print(f"Before: A-185 rows={before_a}, W-202 rows={before_w}")

        async with conn.transaction():
            status = await conn.execute(
                """
                UPDATE auth_user
                SET allowed_warehouses = (
                    SELECT ARRAY(
                        SELECT DISTINCT CASE w
                            WHEN 'A-185' THEN 'A185'
                            WHEN 'W-202' THEN 'W202'
                            ELSE w
                        END
                        FROM unnest(allowed_warehouses) AS w
                    )
                )
                WHERE 'A-185' = ANY(allowed_warehouses) OR 'W-202' = ANY(allowed_warehouses)
                """)
        print(f"UPDATE: {status}")

        after_a = await conn.fetchval("SELECT COUNT(*) FROM auth_user WHERE 'A-185' = ANY(allowed_warehouses)")
        after_w = await conn.fetchval("SELECT COUNT(*) FROM auth_user WHERE 'W-202' = ANY(allowed_warehouses)")
        print(f"After:  A-185 rows={after_a}, W-202 rows={after_w}")

        distinct = [r["val"] for r in await conn.fetch(
            "SELECT DISTINCT w AS val FROM auth_user, LATERAL unnest(COALESCE(allowed_warehouses, ARRAY[]::text[])) w ORDER BY w")]
        print(f"Distinct allowed_warehouses now: {distinct}")
    finally:
        await conn.close()


asyncio.run(main())
