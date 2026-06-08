import asyncio, os, asyncpg
from dotenv import load_dotenv
load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    cols = ["no_of_cartons", "weight_kg", "total_inventory_kgs", "last_purchase_rate", "value", "inward_dt"]
    rows = await c.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='cfpl_cold_stocks' "
        "AND column_name = ANY($1::text[]) ORDER BY column_name",
        cols,
    )
    for r in rows:
        print(f"  {r['column_name']:22} {r['data_type']}")
    await c.close()


asyncio.run(main())
