"""Read-only readiness probe for the NPD requisition page.

Checks the live DB for: the `description` column (058), the PK column on
sample_requisitions (057 swap), and that requestor_user_id / warehouse exist.
No writes."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])

    has_desc = await c.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name='sample_requisitions' AND column_name='description')")

    pk = await c.fetch(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid='sample_requisitions'::regclass AND i.indisprimary")
    pk_cols = [r["attname"] for r in pk]

    cols = await c.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='sample_requisitions' "
        "AND column_name = ANY($1::text[])",
        ["request_id", "requestor_user_id", "warehouse", "npd_target_name", "quantity"])
    present = {r["column_name"] for r in cols}

    await c.close()

    print("description column exists:", has_desc)
    print("primary key columns     :", pk_cols)
    print("required cols present   :", sorted(present))
    print()
    print("READY" if has_desc else "BLOCKED: run migration 058 (add description column)")


asyncio.run(main())
