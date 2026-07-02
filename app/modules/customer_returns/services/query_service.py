"""Customer-Returns read side: column constants, row mappers, pure helpers.
Async list/get functions are added in later tasks of the same module.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

HEADER_COLS = (
    "rtv_id, rtv_date, factory_unit, customer, invoice_number, challan_no, dn_no, "
    "conversion, sales_poc, sales_poc_email, business_head, remark, vehicle_number, "
    "transporter_name, driver_name, inward_manager, status, created_by, created_ts, updated_at"
)
LINE_COLS = (
    "rtv_id, item_description, material_type, item_category, sub_category, uom, qty, rate, "
    "value, net_weight, carton_weight, lot_number, item_mark, spl_remarks, vakkal, created_at, updated_at"
)
BOX_COLS = (
    "rtv_id, article_description, box_number, box_id, uom, conversion, lot_number, item_mark, "
    "spl_remarks, vakkal, net_weight, gross_weight, count, created_at, updated_at"
)


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _line_value(qty: int, rate: float, raw: Any) -> float:
    """Use the supplied value when > 0, else compute qty*rate (source rule)."""
    v = _to_float(raw)
    return v if (v is not None and v > 0) else qty * rate


def _num_str(v: Any) -> str:
    """Serialize a numeric DB value as a string, defaulting to '0'.

    Integral floats/Decimals render without a trailing '.0' ("40", not "40.0")
    and never use scientific notation.
    """
    if v is None:
        return "0"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return ("%f" % v).rstrip("0").rstrip(".")
    if isinstance(v, Decimal):
        s = format(v, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def _convert_date(s: Optional[str]) -> Optional[date]:
    """Parse a DD-MM-YYYY filter string; 400 on bad format; None passes through."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d-%m-%Y").date()
    except ValueError:
        raise HTTPException(
            400,
            detail={"error": "invalid_date", "message": "date must be DD-MM-YYYY",
                    "details": {"value": s}},
        )


def _map_header_row(r: dict) -> dict:
    return {
        "rtv_id": r.get("rtv_id"),
        "rtv_date": r.get("rtv_date"),
        "factory_unit": r.get("factory_unit") or "",
        "customer": r.get("customer") or "",
        "invoice_number": r.get("invoice_number"),
        "challan_no": r.get("challan_no"),
        "dn_no": r.get("dn_no"),
        "conversion": _num_str(r.get("conversion")),
        "sales_poc": r.get("sales_poc"),
        "sales_poc_email": r.get("sales_poc_email"),
        "business_head": r.get("business_head"),
        "remark": r.get("remark"),
        "vehicle_number": r.get("vehicle_number"),
        "transporter_name": r.get("transporter_name"),
        "driver_name": r.get("driver_name"),
        "inward_manager": r.get("inward_manager"),
        "status": r.get("status") or "Pending",
        "created_by": r.get("created_by"),
        "created_ts": r.get("created_ts"),
        "updated_at": r.get("updated_at"),
    }


def _map_line_row(r: dict) -> dict:
    return {
        "rtv_id": r.get("rtv_id"),
        "item_description": r.get("item_description") or "",
        "material_type": r.get("material_type") or "",
        "item_category": r.get("item_category") or "",
        "sub_category": r.get("sub_category") or "",
        "uom": r.get("uom") or "",
        "qty": _num_str(r.get("qty")),
        "rate": _num_str(r.get("rate")),
        "value": _num_str(r.get("value")),
        "net_weight": _num_str(r.get("net_weight")),
        "carton_weight": _num_str(r.get("carton_weight")),
        "lot_number": r.get("lot_number"),
        "item_mark": r.get("item_mark"),
        "spl_remarks": r.get("spl_remarks"),
        "vakkal": r.get("vakkal"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


def _map_box_row(r: dict) -> dict:
    return {
        "rtv_id": r.get("rtv_id"),
        "article_description": r.get("article_description") or "",
        "box_number": r.get("box_number"),
        "box_id": r.get("box_id"),
        "uom": r.get("uom"),
        "conversion": None if r.get("conversion") is None else str(r.get("conversion")),
        "lot_number": r.get("lot_number"),
        "item_mark": r.get("item_mark"),
        "spl_remarks": r.get("spl_remarks"),
        "vakkal": r.get("vakkal"),
        "net_weight": _num_str(r.get("net_weight")),
        "gross_weight": _num_str(r.get("gross_weight")),
        "count": r.get("count"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
    }


from app.modules.customer_returns.tables import cr_table_names


async def _fetch_lines(conn, tables: dict, cr_id: str) -> list:
    rows = await conn.fetch(
        f"SELECT {LINE_COLS} FROM {tables['lines']} WHERE rtv_id = $1 ORDER BY item_description",
        cr_id,
    )
    return [_map_line_row(dict(r)) for r in rows]


async def _fetch_boxes(conn, tables: dict, cr_id: str) -> list:
    rows = await conn.fetch(
        f"SELECT {BOX_COLS} FROM {tables['boxes']} WHERE rtv_id = $1 "
        "ORDER BY article_description, box_number",
        cr_id,
    )
    return [_map_box_row(dict(r)) for r in rows]


async def get_cr(conn, company: str, cr_id: str) -> dict:
    tables = cr_table_names(company)
    hdr = await conn.fetchrow(
        f"SELECT {HEADER_COLS} FROM {tables['header']} WHERE rtv_id = $1", cr_id
    )
    if not hdr:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}",
                    "details": {"rtv_id": cr_id}},
        )
    result = _map_header_row(dict(hdr))
    result["lines"] = await _fetch_lines(conn, tables, cr_id)
    result["boxes"] = await _fetch_boxes(conn, tables, cr_id)
    return result


# Whitelisted sort columns -> real column names (invalid falls back to created_ts).
_SORTABLE = {
    "created_ts": "created_ts",
    "rtv_date": "rtv_date",
    "customer": "customer",
    "factory_unit": "factory_unit",
    "status": "status",
    "rtv_id": "rtv_id",
}


async def list_crs(conn, *, company: str, page: int, per_page: int,
                   status: Optional[str] = None, factory_unit: Optional[str] = None,
                   customer: Optional[str] = None, from_date: Optional[str] = None,
                   to_date: Optional[str] = None, sort_by: str = "created_ts",
                   sort_order: str = "desc") -> dict:
    tables = cr_table_names(company)
    clauses: list[str] = ["1=1"]
    args: list[Any] = []
    if status:
        args.append(status); clauses.append(f"h.status = ${len(args)}")
    if factory_unit:
        args.append(factory_unit); clauses.append(f"h.factory_unit = ${len(args)}")
    if customer:
        args.append(f"%{customer}%"); clauses.append(f"h.customer ILIKE ${len(args)}")
    df = _convert_date(from_date)
    if df:
        args.append(df); clauses.append(f"h.rtv_date >= ${len(args)}")
    dt = _convert_date(to_date)
    if dt:
        args.append(dt); clauses.append(f"h.rtv_date < (${len(args)}::date + 1)")
    where = " AND ".join(clauses)

    total = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['header']} h WHERE {where}", *args
    )

    col = _SORTABLE.get(sort_by, "created_ts")
    direction = "ASC" if str(sort_order).lower() == "asc" else "DESC"
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    offset = (page - 1) * per_page

    rows = await conn.fetch(
        f"""
        SELECT {HEADER_COLS},
               (SELECT COUNT(*) FROM {tables['lines']} l WHERE l.rtv_id = h.rtv_id) AS items_count,
               (SELECT COUNT(*) FROM {tables['boxes']} b WHERE b.rtv_id = h.rtv_id) AS boxes_count,
               (SELECT COALESCE(SUM(l.qty),0) FROM {tables['lines']} l WHERE l.rtv_id = h.rtv_id) AS total_qty,
               (SELECT COALESCE(SUM(b.net_weight),0) FROM {tables['boxes']} b WHERE b.rtv_id = h.rtv_id) AS total_net_weight
          FROM {tables['header']} h
         WHERE {where}
         ORDER BY h.{col} {direction}
         LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, per_page, offset,
    )
    records = []
    for r in rows:
        d = dict(r)
        item = _map_header_row(d)
        item["items_count"] = int(d.get("items_count") or 0)
        item["boxes_count"] = int(d.get("boxes_count") or 0)
        item["total_qty"] = int(d.get("total_qty") or 0)
        item["total_net_weight"] = float(d.get("total_net_weight") or 0)
        records.append(item)

    total = int(total or 0)
    total_pages = (total + per_page - 1) // per_page
    return {"records": records, "total": total, "page": page,
            "per_page": per_page, "total_pages": total_pages}
