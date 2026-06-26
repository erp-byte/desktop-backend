"""Cold-storage stock search + FIFO box pick (doc 08 cold picker).

Read-only asyncpg ports of cold_storage_server.search_cold_storage_stocks / pick_boxes.
The cold tables (cfpl_cold_stocks / cdpl_cold_stocks) are externally managed; each query
is guarded by to_regclass so it returns empty where the tables are absent (e.g. this dev
DB) rather than erroring.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

_COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")


async def _table_exists(conn, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"))


def _get_cold_table(company: Optional[str]) -> str:
    return "cdpl_cold_stocks" if (company or "").strip().lower() == "cdpl" else "cfpl_cold_stocks"


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_SEARCH_SQL = """
    SELECT MIN(id) AS id,
           MIN(inward_dt) AS inward_dt,
           MIN(unit) AS unit,
           COALESCE(inward_no, '') AS inward_no,
           item_description,
           COALESCE(item_mark, '') AS item_mark,
           MAX(vakkal) AS vakkal,
           COALESCE(lot_no, '') AS lot_no,
           SUM(no_of_cartons) AS no_of_cartons,
           MIN(weight_kg) AS weight_kg,
           SUM(COALESCE(no_of_cartons, 0) * COALESCE(weight_kg, 0)) AS total_inventory_kgs,
           MAX(group_name) AS group_name,
           COALESCE(storage_location, '') AS storage_location,
           MAX(exporter) AS exporter,
           MIN(last_purchase_rate) AS last_purchase_rate,
           SUM(value) AS value,
           MIN(box_id) AS box_id,
           MIN(transaction_no) AS transaction_no
    FROM {table}
    WHERE {where_sql}
    GROUP BY item_description, COALESCE(lot_no, ''), COALESCE(inward_no, ''),
             COALESCE(item_mark, ''), COALESCE(storage_location, ''), COALESCE(unit, '')
    ORDER BY MIN(inward_dt) ASC, MIN(id) ASC
    LIMIT {limit_ph}
"""


async def search_cold_storage_stocks(
    conn, *, lot_no=None, item_description=None, group_name=None, inward_dt=None,
    unit=None, storage_location=None, q=None, limit=50,
) -> dict:
    """Search cfpl_cold_stocks first, then cdpl_cold_stocks; each result carries its REAL
    source company so the picker + deduction hit the correct cold table."""
    clauses: list = []
    args: list = []

    def add(clause_tmpl: str, value):
        args.append(value)
        clauses.append(clause_tmpl.format(n=len(args)))

    if lot_no:
        add("CAST(lot_no AS TEXT) ILIKE ${n}", f"%{lot_no}%")
    if item_description:
        add("item_description ILIKE ${n}", f"%{item_description}%")
    if group_name:
        add("group_name ILIKE ${n}", f"%{group_name}%")
    if inward_dt:
        add("CAST(inward_dt AS TEXT) ILIKE ${n}", f"%{inward_dt}%")
    if unit:
        add("unit ILIKE ${n}", f"%{unit}%")
    if storage_location:
        sl = storage_location.strip()
        low = sl.lower()
        # Savla D-39/D-514 may be stored split (storage_location='Savla' + unit='D-39'/'D-514').
        if low in ("savla d-39", "savla d-514"):
            sub = "D-39" if low == "savla d-39" else "D-514"
            args.append(sl)
            clauses.append(
                f"(storage_location ILIKE ${len(args)} OR (storage_location ILIKE 'Savla' "
                f"AND unit ILIKE '{sub}'))")
        else:
            add("storage_location ILIKE ${n}", sl)
    if q:
        args.append(f"%{q}%")
        clauses.append(
            f"(item_description ILIKE ${len(args)} OR group_name ILIKE ${len(args)} "
            f"OR CAST(lot_no AS TEXT) ILIKE ${len(args)})")

    where_sql = " AND ".join(clauses) if clauses else "TRUE"
    args.append(int(limit))
    limit_ph = f"${len(args)}"

    rows: list = []
    source_company = None
    for tbl in _COLD_TABLES:
        if not await _table_exists(conn, tbl):
            continue
        rows = await conn.fetch(_SEARCH_SQL.format(table=tbl, where_sql=where_sql, limit_ph=limit_ph), *args)
        if rows:
            source_company = "cfpl" if tbl == "cfpl_cold_stocks" else "cdpl"
            break

    results = [{
        "id": r["id"],
        "inward_dt": str(r["inward_dt"]) if r["inward_dt"] else None,
        "unit": r["unit"],
        "inward_no": r["inward_no"],
        "item_description": r["item_description"],
        "item_mark": r["item_mark"],
        "vakkal": r["vakkal"],
        "lot_no": r["lot_no"],
        "net_qty_on_cartons": _f(r["no_of_cartons"]),
        "weight_kg": _f(r["weight_kg"]),
        "total_inventory_kgs": _f(r["total_inventory_kgs"]),
        "group_name": r["group_name"],
        "storage_location": r["storage_location"],
        "stock": None,
        "exporter": r["exporter"],
        "last_purchase_rate": _f(r["last_purchase_rate"]),
        "value": _f(r["value"]),
        "box_id": r["box_id"],
        "transaction_no": r["transaction_no"],
        "company": source_company,
    } for r in rows]

    return {"results": results, "total": len(results)}


async def pick_boxes(conn, *, company: str, item_description: str, lot_no: str,
                     inward_no: str, qty: int) -> dict:
    """Return individual box rows in FIFO order (id ASC) for item+lot+inward_no — one
    unique box_id per box, so the dispatch never collapses many boxes to one id."""
    table = _get_cold_table(company)
    if not await _table_exists(conn, table):
        raise HTTPException(404, f"Cold table {table} not available")
    rows = await conn.fetch(
        f"""
        SELECT id, box_id, transaction_no, weight_kg, item_mark, inward_dt, unit, inward_no,
               item_description, vakkal, lot_no, no_of_cartons, total_inventory_kgs,
               group_name, storage_location, exporter, last_purchase_rate, value
        FROM {table}
        WHERE item_description = $1 AND CAST(lot_no AS TEXT) = $2 AND COALESCE(inward_no, '') = $3
        ORDER BY id ASC
        LIMIT $4
        """,
        item_description, lot_no, inward_no, int(qty),
    )
    return {"boxes": [{
        "id": r["id"],
        "box_id": r["box_id"],
        "transaction_no": r["transaction_no"],
        "weight_kg": _f(r["weight_kg"]) or 0,
        "item_mark": r["item_mark"] or "",
        "inward_dt": str(r["inward_dt"]) if r["inward_dt"] else "",
        "unit": r["unit"] or "",
        "inward_no": r["inward_no"] or "",
        "item_description": r["item_description"] or "",
        "vakkal": r["vakkal"] or "",
        "lot_no": str(r["lot_no"]) if r["lot_no"] else "",
        "no_of_cartons": int(r["no_of_cartons"]) if r["no_of_cartons"] else 0,
        "total_inventory_kgs": _f(r["total_inventory_kgs"]) or 0,
        "group_name": r["group_name"] or "",
        "storage_location": r["storage_location"] or "",
        "exporter": r["exporter"] or "",
        "last_purchase_rate": _f(r["last_purchase_rate"]) or 0,
        "value": _f(r["value"]) or 0,
    } for r in rows]}
