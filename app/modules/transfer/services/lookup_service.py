"""Box lookups for the transfer-OUT form (doc 07).

Read-only ports of inward_tools.get_box_by_number / get_box_by_box_id and
interunit_tools.get_bulk_entry_box (asyncpg). They back the form's three box
ingestion paths:
  - manual entry (box_number + transaction_no)          → get_box_by_number
  - new "TR-" QR (box_id + transaction_no)               → get_box_by_box_id
  - bulk-entry "BE-" QR (box_id + transaction_no)        → get_bulk_entry_box

All return {"success": True, "box": {...}} on a hit, or raise HTTPException(404).
The legacy "TX/CONS" QR path (reference GET /inward/{company}/{txn}) is NOT ported
— the rebuild has no inward module — so that one QR format is unsupported here.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.transfer.services.stock_service import _table_exists


def _prefix(company: str) -> str:
    return "cdpl" if (company or "").strip().lower() == "cdpl" else "cfpl"


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _box_from_v2(row: dict) -> dict:
    """Shape a {prefix}_boxes_v2 (+ articles_v2 + sku) row into the box payload."""
    return {
        "box_id": row.get("box_id"),
        "transaction_no": row.get("transaction_no"),
        "box_number": row.get("box_number"),
        "article_description": row.get("article_description"),
        "item_description": row.get("item_description") or row.get("article_description"),
        "sku_id": row.get("sku_id"),
        "item_category": row.get("item_category"),
        "sub_category": row.get("sub_category"),
        "material_type": row.get("material_type") or row.get("sku_material_type"),
        "net_weight": _f(row.get("net_weight")),
        "gross_weight": _f(row.get("gross_weight")),
        "lot_number": row.get("lot_number") or row.get("article_lot_number"),
        "batch_number": row.get("batch_number"),
        "uom": row.get("uom"),
        "quantity_units": row.get("quantity_units"),
        "packaging_type": row.get("packaging_type"),
        "quality_grade": row.get("quality_grade"),
        "count": row.get("count"),
    }


_V2_SELECT = """
    SELECT b.*, a.sku_id, a.item_description, a.item_category, a.sub_category,
           a.material_type, a.uom, a.lot_number AS article_lot_number,
           a.quantity_units, a.quality_grade,
           s.material_type AS sku_material_type
    FROM {box} b
    LEFT JOIN {art} a
        ON b.transaction_no = a.transaction_no
       AND b.article_description = a.item_description
    LEFT JOIN {sku} s ON a.sku_id = s.id
    WHERE {pred}
    LIMIT 1
"""


async def _query_v2(conn, prefix: str, pred: str, *args) -> dict | None:
    box, art, sku = f"{prefix}_boxes_v2", f"{prefix}_articles_v2", f"{prefix}sku"
    if not await _table_exists(conn, box):
        return None
    row = await conn.fetchrow(_V2_SELECT.format(box=box, art=art, sku=sku, pred=pred), *args)
    return _box_from_v2(dict(row)) if row else None


async def _query_bulk(conn, prefix: str, box_id: str, transaction_no: str) -> dict | None:
    table = f"{prefix}_bulk_entry_boxes"
    if not await _table_exists(conn, table):
        return None
    row = await conn.fetchrow(
        f"""SELECT transaction_no, box_id, article_description, lot_number,
                   net_weight, gross_weight, box_number
            FROM {table} WHERE box_id = $1 AND transaction_no = $2 LIMIT 1""",
        box_id, transaction_no,
    )
    if not row:
        return None
    r = dict(row)
    return {
        "box_id": r.get("box_id"),
        "transaction_no": r.get("transaction_no"),
        "box_number": r.get("box_number") or 0,
        "article_description": r.get("article_description"),
        "item_description": r.get("article_description"),
        "lot_number": r.get("lot_number"),
        "net_weight": _f(r.get("net_weight")),
        "gross_weight": _f(r.get("gross_weight")),
        "material_type": "RM",
        "item_category": "",
        "sub_category": "",
        "uom": "BAG",
        "batch_number": "",
        "sku_id": None,
    }


async def get_box_by_number(conn, company: str, box_number: int, transaction_no: str) -> dict:
    """Manual box entry: look up by box_number + transaction_no. Tries the named
    company's boxes_v2 first, then the other (company-agnostic UI)."""
    primary = _prefix(company)
    for prefix in (primary, "cdpl" if primary == "cfpl" else "cfpl"):
        box = await _query_v2(conn, prefix, "b.box_number = $1 AND b.transaction_no = $2",
                              box_number, transaction_no)
        if box:
            return {"success": True, "box": box}
    raise HTTPException(404, f"Box #{box_number} with transaction_no '{transaction_no}' not found")


async def get_box_by_box_id(conn, company: str, box_id: str, transaction_no: str) -> dict:
    """New "TR-" QR: look up by box_id + transaction_no. Searches both companies'
    boxes_v2, then falls back to bulk_entry_boxes."""
    for prefix in ("cfpl", "cdpl"):
        box = await _query_v2(conn, prefix, "b.box_id = $1 AND b.transaction_no = $2",
                              box_id, transaction_no)
        if box:
            return {"success": True, "box": box}
    for prefix in ("cfpl", "cdpl"):
        box = await _query_bulk(conn, prefix, box_id, transaction_no)
        if box:
            return {"success": True, "box": box}
    raise HTTPException(404, f"Box '{box_id}' with transaction '{transaction_no}' not found")


async def get_bulk_entry_box(conn, company: str, box_id: str, transaction_no: str) -> dict:
    """Bulk-entry "BE-" QR: look up by box_id + transaction_no in bulk_entry_boxes
    (both companies)."""
    for prefix in ("cfpl", "cdpl"):
        box = await _query_bulk(conn, prefix, box_id, transaction_no)
        if box:
            return {"success": True, "box": box}
    raise HTTPException(404, f"Box with box_id '{box_id}' and transaction_no '{transaction_no}' not found in bulk entry boxes")
