"""READ-ONLY: confirm cfpl/cdpl_cold_stocks have every column pick_from_pending
and _insert_cold_storage_items write to. Pure information_schema lookup."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

REQUIRED = [
    "inward_dt", "unit", "inward_no", "item_description", "item_mark", "vakkal",
    "lot_no", "no_of_cartons", "weight_kg", "total_inventory_kgs", "group_name",
    "item_subgroup", "storage_location", "exporter", "last_purchase_rate", "value",
    "box_id", "transaction_no", "spl_remarks",
]


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        for tbl in ("cfpl_cold_stocks", "cdpl_cold_stocks"):
            exists = await conn.fetchval("SELECT to_regclass($1)", f"public.{tbl}")
            if not exists:
                print(f"{tbl}: TABLE MISSING")
                continue
            cols = {r["column_name"] for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1", tbl)}
            missing = [c for c in REQUIRED if c not in cols]
            print(f"{tbl}: {'ALL PRESENT' if not missing else 'MISSING -> ' + str(missing)} ({len(cols)} cols total)")
    finally:
        await conn.close()


asyncio.run(main())
