"""Probe: inspect job card PRD-2026-0023/2 and reproduce the V2 output save."""
import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        # 1) Find the JC
        jc = await conn.fetchrow(
            """
            SELECT job_card_id, job_card_number, stage, step_number, status,
                   is_locked, batch_size_kg, fg_sku_name, entity, prod_order_id
            FROM job_card
            WHERE job_card_number = $1
            """,
            "PRD-2026-0023/2",
        )
        print("--- Job card ---")
        print(dict(jc) if jc else "NOT FOUND")
        if not jc:
            return

        jc_id = jc["job_card_id"]

        # 2) Existing output row
        out = await conn.fetchrow(
            "SELECT * FROM job_card_output WHERE job_card_id = $1", jc_id
        )
        print("\n--- Existing output row ---")
        print(dict(out) if out else None)

        # 3) Existing byproducts
        bp = await conn.fetch(
            "SELECT * FROM job_card_byproduct WHERE job_card_id = $1", jc_id
        )
        print("\n--- Existing byproducts ---")
        for r in bp:
            print(dict(r))

        # 4) Existing balance materials
        bm = await conn.fetch(
            "SELECT * FROM job_card_balance_material WHERE job_card_id = $1", jc_id
        )
        print("\n--- Existing balance materials ---")
        for r in bm:
            print(dict(r))

        # 5) Existing loss recon
        lr = await conn.fetch(
            "SELECT * FROM job_card_loss_reconciliation WHERE job_card_id = $1 AND deleted_at IS NULL",
            jc_id,
        )
        print("\n--- Existing loss recon ---")
        for r in lr:
            print(dict(r))

        # 6) Show columns of job_card_output for reference
        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'job_card_output' ORDER BY ordinal_position
            """
        )
        print("\n--- job_card_output schema ---")
        for r in cols:
            print(dict(r))

        # 7) Show CHECK constraints on job_card_balance_material
        chk = await conn.fetch(
            """
            SELECT pg_get_constraintdef(c.oid) AS def
            FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            WHERE t.relname IN (
                'job_card_balance_material', 'job_card_byproduct', 'job_card_output',
                'job_card_loss_reconciliation'
            )
            ORDER BY t.relname
            """
        )
        print("\n--- Constraints on output tables ---")
        for r in chk:
            print(r["def"])

    finally:
        await conn.close()


asyncio.run(main())
