"""Read-only introspection of the dev-jc tables on the LIVE DB.

Confirms field parity (FE/schema/service ↔ DB columns) and which migrations
(061 phase_id BIGINT, 062 accounting, 063 line phase_id) are actually applied.
Pure SELECT against information_schema — touches no data."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def cols(c, table):
    rows = await c.fetch(
        """SELECT column_name, data_type, is_nullable
             FROM information_schema.columns
            WHERE table_name = $1 ORDER BY ordinal_position""", table)
    return {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for t in ("npd_dev_job_cards", "npd_dev_job_card_lines", "npd_dev_job_card_phases"):
            cmap = await cols(c, t)
            if not cmap:
                print(f"\n### {t}: DOES NOT EXIST")
                continue
            print(f"\n### {t}")
            for name, (dt, nul) in cmap.items():
                print(f"   {name:24} {dt:28} {'NULL' if nul=='YES' else 'NOT NULL'}")

        # Migration tells
        lines = await cols(c, "npd_dev_job_card_lines")
        phases = await cols(c, "npd_dev_job_card_phases")
        jc = await cols(c, "npd_dev_job_cards")
        print("\n=== migration state ===")
        print("060 jc.id BIGINT          :", jc.get("id"))
        print("061 phases table exists   :", bool(phases))
        print("061 phases.phase_id type  :", phases.get("phase_id"))
        print("062 phases.output_qty     :", "PRESENT" if "output_qty" in phases else "MISSING")
        print("062 phases.yield_pct      :", "PRESENT" if "yield_pct" in phases else "MISSING")
        print("063 lines.phase_id        :", "PRESENT" if "phase_id" in lines else "MISSING")
        print("    lines.phase_id type   :", lines.get("phase_id"))
        # recipe line field parity vs _insert_lines / NpdLineIn
        need = ["dev_jc_id", "phase_id", "sku_id", "sku_name", "qty", "uom", "item_type",
                "ownership", "is_off_master", "customer_lot_ref", "received_qty", "line_order", "notes"]
        missing = [n for n in need if n not in lines]
        print("\nrecipe-line columns required by _insert_lines, MISSING:", missing or "none")
    finally:
        await c.close()


asyncio.run(main())
