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


def _box_from_row(r: dict) -> dict:
    """Shape a unified search row (see _V2_BRANCH / _BULK_BRANCH) into the box payload.
    quantity_units/packaging_type/quality_grade/count are unconsumed by the scanner, so
    they're no longer selected — kept in the shape as null for response compatibility."""
    return {
        "box_id": r.get("box_id"),
        "transaction_no": r.get("transaction_no"),
        "box_number": r.get("box_number"),
        "article_description": r.get("article_description"),
        "item_description": r.get("item_description") or r.get("article_description"),
        "sku_id": r.get("sku_id"),
        "item_category": r.get("item_category"),
        "sub_category": r.get("sub_category"),
        "material_type": r.get("material_type"),
        "net_weight": _f(r.get("net_weight")),
        "gross_weight": _f(r.get("gross_weight")),
        "lot_number": r.get("lot_number"),
        "batch_number": r.get("batch_number"),
        "uom": r.get("uom"),
        "quantity_units": None,
        "packaging_type": None,
        "quality_grade": None,
        "count": None,
    }


# One UNION-ALL branch per candidate table, projecting the SAME columns/types in the SAME
# order so the branches union cleanly. Casts pin the types (a NULL/literal in one branch must
# match a real column in another). `{idcol}` is box_id or box_number; every branch binds
# $1 (the id) and $2 (transaction_no). A `_prio` literal drives ORDER BY so v2 wins over bulk.
_V2_BRANCH = """
    SELECT b.box_id::text AS box_id, b.transaction_no::text AS transaction_no,
           b.box_number::bigint AS box_number, b.article_description::text AS article_description,
           a.item_description::text AS item_description, a.sku_id::bigint AS sku_id,
           a.item_category::text AS item_category, a.sub_category::text AS sub_category,
           a.material_type::text AS material_type,
           b.net_weight::numeric AS net_weight, b.gross_weight::numeric AS gross_weight,
           COALESCE(NULLIF(b.lot_number, ''), a.lot_number)::text AS lot_number,
           b.batch_number::text AS batch_number, a.uom::text AS uom,
           {prio} AS _prio
    FROM {box} b
    LEFT JOIN {art} a
      ON b.transaction_no = a.transaction_no
     AND b.article_description = a.item_description
    WHERE b.{idcol} = $1 AND b.transaction_no = $2
"""

_BULK_BRANCH = """
    SELECT box_id::text AS box_id, transaction_no::text AS transaction_no,
           COALESCE(box_number, 0)::bigint AS box_number, article_description::text AS article_description,
           article_description::text AS item_description, NULL::bigint AS sku_id,
           ''::text AS item_category, ''::text AS sub_category, 'RM'::text AS material_type,
           net_weight::numeric AS net_weight, gross_weight::numeric AS gross_weight,
           lot_number::text AS lot_number, ''::text AS batch_number, 'BAG'::text AS uom,
           {prio} AS _prio
    FROM {table}
    WHERE box_id = $1 AND transaction_no = $2
"""


# Process-lifetime existence cache. `to_regclass` is a catalog round-trip and the box tables
# don't come/go at runtime, so each table is probed once instead of on every scan.
# ponytail: restart the process to pick up a newly-created box table (rare; acceptable ceiling).
_EXISTS: dict[str, bool] = {}


async def _exists(conn, table: str) -> bool:
    hit = _EXISTS.get(table)
    if hit is None:
        hit = await _table_exists(conn, table)
        _EXISTS[table] = hit
    return hit


async def _search(conn, *, id_col: str, id_val, transaction_no: str,
                  prefixes: tuple[str, ...], include_v2: bool, include_bulk: bool) -> dict | None:
    """Single-round-trip box search: UNION ALL over the candidate tables that exist, ordered
    by priority (v2 before bulk, in `prefixes` order), LIMIT 1. Returns the top hit or None."""
    branches: list[str] = []
    prio = 0
    if include_v2:
        for prefix in prefixes:
            box, art = f"{prefix}_boxes_v2", f"{prefix}_articles_v2"
            if await _exists(conn, box):
                prio += 1
                branches.append(_V2_BRANCH.format(box=box, art=art, idcol=id_col, prio=prio))
    if include_bulk:
        for prefix in prefixes:
            table = f"{prefix}_bulk_entry_boxes"
            if await _exists(conn, table):
                prio += 1
                branches.append(_BULK_BRANCH.format(table=table, prio=prio))
    if not branches:
        return None
    sql = "SELECT * FROM (" + " UNION ALL ".join(branches) + ") u ORDER BY _prio LIMIT 1"
    row = await conn.fetchrow(sql, id_val, transaction_no)
    return _box_from_row(dict(row)) if row else None


async def get_box_by_number(conn, company: str, box_number: int, transaction_no: str) -> dict:
    """Manual box entry: box_number + transaction_no. Named company's boxes_v2 first, then the
    other (company-agnostic UI). One query across the existing tables."""
    primary = _prefix(company)
    other = "cdpl" if primary == "cfpl" else "cfpl"
    box = await _search(conn, id_col="box_number", id_val=box_number, transaction_no=transaction_no,
                        prefixes=(primary, other), include_v2=True, include_bulk=False)
    if box:
        return {"success": True, "box": box}
    raise HTTPException(404, f"Box #{box_number} with transaction_no '{transaction_no}' not found")


async def get_box_by_box_id(conn, company: str, box_id: str, transaction_no: str) -> dict:
    """New "TR-" QR: box_id + transaction_no. Both companies' boxes_v2 first, then
    bulk_entry_boxes — all in one query (v2 wins over bulk via _prio)."""
    box = await _search(conn, id_col="box_id", id_val=box_id, transaction_no=transaction_no,
                        prefixes=("cfpl", "cdpl"), include_v2=True, include_bulk=True)
    if box:
        return {"success": True, "box": box}
    raise HTTPException(404, f"Box '{box_id}' with transaction '{transaction_no}' not found")


async def get_bulk_entry_box(conn, company: str, box_id: str, transaction_no: str) -> dict:
    """Bulk-entry "BE-" QR: box_id + transaction_no in bulk_entry_boxes (both companies)."""
    box = await _search(conn, id_col="box_id", id_val=box_id, transaction_no=transaction_no,
                        prefixes=("cfpl", "cdpl"), include_v2=False, include_bulk=True)
    if box:
        return {"success": True, "box": box}
    raise HTTPException(404, f"Box with box_id '{box_id}' and transaction_no '{transaction_no}' not found in bulk entry boxes")
