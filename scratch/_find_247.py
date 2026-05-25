import asyncio, os
from pathlib import Path
import asyncpg

for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(
            "SELECT so_id, so_number, so_date, customer_name, extraction_status, created_at "
            "FROM so_header "
            "WHERE so_number ILIKE '%247%' "
            "ORDER BY created_at DESC LIMIT 30"
        )
        print(f"Matches containing '247': {len(rows)}")
        for r in rows:
            print(f"  so_id={r['so_id']}  '{r['so_number']}'  date={r['so_date']}  cust={r['customer_name']}  status={r['extraction_status']}  created={r['created_at']}")

        rows2 = await conn.fetch(
            "SELECT so_id, so_number, so_date, customer_name, created_at "
            "FROM so_header WHERE so_number LIKE 'CF-SO/26-27/24%' "
            "ORDER BY so_number"
        )
        print(f"\nAll CF-SO/26-27/24x: {len(rows2)}")
        for r in rows2:
            print(f"  so_id={r['so_id']}  '{r['so_number']}'  date={r['so_date']}  cust={r['customer_name']}")
    finally:
        await conn.close()

asyncio.run(main())
