import asyncio, os, asyncpg
from dotenv import load_dotenv
load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    tbls = await c.fetch(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema='public' AND column_name='allowed_warehouses'")
    for t in tbls:
        tbl = t["table_name"]
        print(f"-- table {tbl} --")
        try:
            rows = await c.fetch(
                f"SELECT DISTINCT w AS val FROM {tbl}, LATERAL unnest(COALESCE(allowed_warehouses, ARRAY[]::text[])) w ORDER BY w")
            vals = [r["val"] for r in rows]
            print(f"   distinct allowed_warehouses values: {vals}")
        except Exception as e:
            print(f"   (could not unnest: {e})")
    await c.close()


asyncio.run(main())
