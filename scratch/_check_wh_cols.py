import asyncio, os, asyncpg
from dotenv import load_dotenv
load_dotenv()
REQ = ["box_id", "transaction_no", "article_description", "lot_number", "net_weight", "gross_weight", "box_number", "count"]


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    for tbl in ("cfpl_boxes_v2", "cdpl_boxes_v2", "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes"):
        ex = await c.fetchval("SELECT to_regclass($1)", f"public.{tbl}")
        if not ex:
            print(f"{tbl}: MISSING"); continue
        cols = {r["column_name"] for r in await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1", tbl)}
        missing = [x for x in REQ if x not in cols]
        print(f"{tbl}: {'ALL PRESENT' if not missing else 'MISSING -> ' + str(missing)}")
    await c.close()


asyncio.run(main())
