"""Inspect current bom_header / bom_line counts before BOM Excel re-import."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]


async def main() -> None:
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            h_total = await c.fetchval("SELECT COUNT(*) FROM bom_header")
            h_cust_null = await c.fetchval("SELECT COUNT(*) FROM bom_header WHERE customer_name IS NULL")
            h_cust = await c.fetchval("SELECT COUNT(*) FROM bom_header WHERE customer_name IS NOT NULL")
            l_total = await c.fetchval("SELECT COUNT(*) FROM bom_line")
            by_entity = await c.fetch("SELECT entity, COUNT(*) FROM bom_header GROUP BY entity")
            no_lines = await c.fetchval("""
                SELECT COUNT(*) FROM bom_header h
                WHERE NOT EXISTS (SELECT 1 FROM bom_line l WHERE l.bom_id = h.bom_id)
            """)
            sample = await c.fetch("""
                SELECT bom_id, fg_sku_name, customer_name, entity
                FROM bom_header ORDER BY bom_id LIMIT 5
            """)
            print(f"bom_header total: {h_total}")
            print(f"  customer_name IS NULL: {h_cust_null}")
            print(f"  customer_name set    : {h_cust}")
            print(f"  by entity            : {[dict(r) for r in by_entity]}")
            print(f"  headers without lines: {no_lines}")
            print(f"bom_line total       : {l_total}")
            print("sample headers:")
            for r in sample:
                print(f"  {dict(r)}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
