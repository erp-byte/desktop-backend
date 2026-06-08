"""Inner-cold transfer list + delete (the dashboard's Inner Cold tab).

Ported from the reference cold_storage_server.py
(`/cold-storage/inner-transfer/list` + delete over `inner_cold_transfer`).
Records are grouped by challan_no, each carrying its relabel lines.
"""
from __future__ import annotations

from fastapi import HTTPException


async def _table_exists(conn) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('public.inner_cold_transfer')"))


async def list_inner_cold(conn, *, page, per_page, scope=None) -> dict:
    if not await _table_exists(conn):
        return {"records": [], "total": 0, "page": page, "per_page": per_page, "total_pages": 0}

    # Warehouse scope (empty/None = unrestricted) filters by from_warehouse.
    where, scope_args = "", []
    if scope:
        scope_args = [[str(w).strip().lower().replace("-", "") for w in scope]]
        where = "WHERE REPLACE(LOWER(from_warehouse),'-','') = ANY($1::text[])"

    total = await conn.fetchval(
        f"SELECT COUNT(DISTINCT challan_no) FROM inner_cold_transfer {where}", *scope_args) or 0
    total_pages = max(1, -(-total // per_page)) if total else 0
    offset = (page - 1) * per_page

    challans = await conn.fetch(
        f"""
        SELECT
            challan_no,
            MIN(transfer_date)   AS transfer_date,
            MIN(from_warehouse)  AS from_warehouse,
            MIN(reason_code)     AS reason_code,
            MIN(remark)          AS remark,
            MIN(status)          AS status,
            COUNT(*)             AS line_count,
            SUM(quantity)        AS total_boxes,
            MIN(created_at)      AS created_at
        FROM inner_cold_transfer
        {where}
        GROUP BY challan_no
        ORDER BY MIN(created_at) DESC
        LIMIT ${len(scope_args) + 1} OFFSET ${len(scope_args) + 2}
        """,
        *scope_args, per_page, offset,
    )

    records = []
    for c in challans:
        cr = dict(c)
        line_rows = await conn.fetch(
            """
            SELECT item_description, item_category, quantity,
                   old_lot_number, new_lot_number, net_weight_kg,
                   new_storage_location
            FROM inner_cold_transfer
            WHERE challan_no = $1
            ORDER BY id
            """,
            cr["challan_no"],
        )
        records.append({
            "challan_no": cr["challan_no"],
            "transfer_date": cr["transfer_date"],
            "from_warehouse": cr["from_warehouse"],
            "reason_code": cr["reason_code"],
            "remark": cr["remark"],
            "status": cr["status"] or "COMPLETED",
            "line_count": cr["line_count"],
            "total_boxes": int(cr["total_boxes"]) if cr["total_boxes"] is not None else None,
            "created_at": str(cr["created_at"]) if cr["created_at"] else None,
            "lines": [
                {
                    "item_description": ln["item_description"],
                    "item_category": ln["item_category"],
                    "quantity": ln["quantity"],
                    "old_lot_number": ln["old_lot_number"],
                    "new_lot_number": ln["new_lot_number"],
                    "net_weight_kg": float(ln["net_weight_kg"]) if ln["net_weight_kg"] else 0,
                    "new_storage_location": ln["new_storage_location"],
                }
                for ln in line_rows
            ],
        })

    return {
        "records": records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


async def delete_inner_cold(conn, challan_no: str) -> dict:
    if not await _table_exists(conn):
        raise HTTPException(404, "Inner cold transfer table not found")
    result = await conn.execute(
        "DELETE FROM inner_cold_transfer WHERE challan_no = $1", challan_no)
    # asyncpg returns a status string like "DELETE 3"; rowcount is the trailing int.
    deleted = int(result.split()[-1]) if result and result.split()[-1].isdigit() else 0
    if deleted == 0:
        raise HTTPException(404, "Inner cold transfer not found")
    return {"success": True, "message": f"Inner cold transfer {challan_no} deleted successfully."}
