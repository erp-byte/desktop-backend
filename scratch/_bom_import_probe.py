"""Import BOM data from 'BOM Details CFPL as on 16-05-26 (1).xlsx' into Postgres.

Strategy:
  - For each (StockItem, BOMName) combo in the Excel file:
      * Treat BOMName as customer_name when BOMName != StockItem, else NULL (generic BOM)
      * Look up an existing bom_header by (fg_sku_name, customer_name); create if missing
        (entity defaults to 'cfpl' for newly created headers — file is "BOM Details CFPL")
      * Replace all bom_line rows for that bom_id with the fresh material list from Excel

Excel columns:
  [0] Stock Item        — FG SKU name
  [1] BOM Name          — customer/variant label
  [2] FG Qty            — units of FG that the BOM yield produces (typically 1; sometimes 10, 1000)
  [3] Raw Material      — material SKU name
  [4] Godown            — Factory | PM Store | APMC
  [5] Type of Item      — Component
  [6] BOM Qty           — material quantity for the FG Qty batch  →  per-unit = BOM_Qty / FG_Qty

Item type rule:
  - material name starts with "PM" or godown == 'PM Store'  →  'pm'
  - otherwise                                                →  'rm'

Idempotent: re-runnable. Header upsert preserves existing bom_id; lines are wiped and rebuilt
per affected bom_id, so re-running converges to the Excel state.

Usage:
    cd d:/Consumption/New/Backend
    python _bom_import_probe.py
"""
import asyncio
import os
from collections import defaultdict
from pathlib import Path

import asyncpg
import openpyxl
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

XLSX_PATH = Path(r"C:\Users\cando\Downloads\Inventory calc\BOM Details CFPL as on 16-05-26 (1).xlsx")
ENTITY = "cfpl"


def _norm(s) -> str | None:
    if s is None:
        return None
    out = str(s).strip()
    return out if out else None


def _is_pm(material: str, godown: str | None) -> str:
    m = material.strip().upper()
    if godown and godown.strip().lower() == "pm store":
        return "pm"
    if m.startswith("PM"):
        return "pm"
    return "rm"


def _uom_for(item_type: str, godown: str | None) -> str:
    # Factory RM materials are stocked in kg; PM is per piece
    if item_type == "pm":
        return "pcs"
    return "kg"


def load_excel() -> dict[tuple[str, str | None], list[dict]]:
    """Return {(fg_name, customer_name_or_None): [line_dict, ...]} preserving Excel order."""
    wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
    ws = wb["BOM of Stock Item"]

    boms: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    skipped_no_material = 0
    skipped_bad_qty = 0

    for r in ws.iter_rows(min_row=3, values_only=True):
        stock_item = _norm(r[0])
        bom_name = _norm(r[1])
        fg_qty = r[2]
        material = _norm(r[3])
        godown = _norm(r[4])
        bom_qty = r[6]

        if not stock_item or not bom_name:
            continue
        if not material:
            skipped_no_material += 1
            continue
        if fg_qty is None or bom_qty is None:
            skipped_bad_qty += 1
            continue
        try:
            fg_qty_f = float(fg_qty)
            bom_qty_f = float(bom_qty)
        except (TypeError, ValueError):
            skipped_bad_qty += 1
            continue
        if fg_qty_f == 0:
            skipped_bad_qty += 1
            continue

        per_unit = round(bom_qty_f / fg_qty_f, 6)
        item_type = _is_pm(material, godown)
        customer = None if bom_name == stock_item else bom_name

        key = (stock_item, customer)
        boms[key].append({
            "material": material,
            "item_type": item_type,
            "quantity_per_unit": per_unit,
            "uom": _uom_for(item_type, godown),
            "godown": godown,
        })

    wb.close()
    print(f"Excel: {len(boms)} BOM variants loaded "
          f"(skipped {skipped_no_material} no-material, {skipped_bad_qty} bad-qty rows)")
    return boms


async def upsert_header(conn, fg_name: str, customer: str | None) -> tuple[int, bool]:
    """Return (bom_id, created_flag)."""
    if customer is None:
        existing = await conn.fetchval(
            "SELECT bom_id FROM bom_header WHERE fg_sku_name = $1 AND customer_name IS NULL LIMIT 1",
            fg_name,
        )
    else:
        existing = await conn.fetchval(
            "SELECT bom_id FROM bom_header WHERE fg_sku_name = $1 AND customer_name = $2 LIMIT 1",
            fg_name, customer,
        )
    if existing is not None:
        return existing, False

    bom_id = await conn.fetchval(
        """
        INSERT INTO bom_header (fg_sku_name, customer_name, entity, is_active, version)
        VALUES ($1, $2, $3, TRUE, 1)
        RETURNING bom_id
        """,
        fg_name, customer, ENTITY,
    )
    return bom_id, True


async def replace_lines(conn, bom_id: int, lines: list[dict]) -> int:
    await conn.execute("DELETE FROM bom_line WHERE bom_id = $1", bom_id)
    inserted = 0
    for idx, ln in enumerate(lines, 1):
        await conn.execute(
            """
            INSERT INTO bom_line (
                bom_id, line_number, material_sku_name, item_type,
                quantity_per_unit, uom, godown, loss_pct
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 0)
            """,
            bom_id, idx, ln["material"], ln["item_type"],
            ln["quantity_per_unit"], ln["uom"], ln["godown"],
        )
        inserted += 1
    return inserted


async def main() -> None:
    boms = load_excel()

    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
    headers_created = 0
    headers_existing = 0
    lines_inserted = 0
    bom_ids_touched: set[int] = set()

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                for (fg_name, customer), lines in boms.items():
                    bom_id, created = await upsert_header(conn, fg_name, customer)
                    if created:
                        headers_created += 1
                    else:
                        headers_existing += 1
                    inserted = await replace_lines(conn, bom_id, lines)
                    lines_inserted += inserted
                    bom_ids_touched.add(bom_id)

        # Post-import verification
        async with pool.acquire() as conn:
            h_total = await conn.fetchval("SELECT COUNT(*) FROM bom_header")
            l_total = await conn.fetchval("SELECT COUNT(*) FROM bom_line")
            h_cust = await conn.fetchval(
                "SELECT COUNT(*) FROM bom_header WHERE customer_name IS NOT NULL"
            )
    finally:
        await pool.close()

    print("-" * 60)
    print(f"Headers: {headers_created} created, {headers_existing} reused "
          f"(touched {len(bom_ids_touched)} bom_ids)")
    print(f"Lines  : {lines_inserted} inserted (replaced for each touched bom_id)")
    print("-" * 60)
    print(f"DB now: bom_header={h_total} (customer-specific={h_cust}), bom_line={l_total}")


if __name__ == "__main__":
    asyncio.run(main())
