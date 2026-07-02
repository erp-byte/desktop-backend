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
