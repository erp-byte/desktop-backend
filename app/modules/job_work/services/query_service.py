"""Read side for Job Work Material Out.

Only the single-record read exists so far — `create_out` reads its result back
through it so a POST response is byte-identical to a later GET (the pattern
customer_returns/create_service uses). The list/search endpoints will reuse
`_map_header` / `_map_line` when they land.

No JSON codec is registered on the pool (see app/db/connection.py), so asyncpg
hands back JSONB columns as `str` — `_json` normalises that.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException

HEADER_COLS = (
    "id, challan_no, job_work_date, from_warehouse, to_party, party_address, "
    "party_state, party_city, party_pin_code, party_contact_company, "
    "party_contact_mobile, party_email, sub_category, contact_person, contact_number, "
    "purpose_of_work, expected_return_date, vehicle_no, driver_name, authorized_person, "
    "remarks, e_way_bill_no, dispatched_through, type, status, dispatch_to, "
    "created_by, created_at, updated_at"
)

LINE_COLS = (
    "id, sl_no, item_description, material_type, item_category, sub_category, "
    "quantity_kgs, quantity_boxes, rate_per_kg, amount, uom, case_pack, net_weight, "
    "total_weight, batch_number, lot_number, manufacturing_date, expiry_date, "
    "line_remarks, cold_unit, item_mark, box_id, transaction_no, cold_storage_snapshot"
)


def _json(v: Any) -> Optional[dict]:
    if v is None or v == "":
        return None
    if isinstance(v, dict):
        return v
    try:
        parsed = json.loads(v)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _f(v: Any) -> float:
    """NUMERIC -> float. Decimal is not JSON-serialisable and the web client
    types these as `number`."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return 0.0


def _i(v: Any) -> int:
    return int(_f(v))


def _s(v: Any) -> Optional[str]:
    """Timestamps to ISO strings; everything else through untouched. The weight
    columns are already VARCHAR in this schema, so nothing else needs coercing."""
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _map_line(row: dict) -> dict:
    return {
        "id": row["id"],
        "sl_no": row.get("sl_no"),
        "item_description": row.get("item_description"),
        "material_type": row.get("material_type"),
        "item_category": row.get("item_category"),
        "sub_category": row.get("sub_category"),
        "quantity_kgs": _f(row.get("quantity_kgs")),
        "quantity_boxes": _i(row.get("quantity_boxes")),
        "rate_per_kg": _f(row.get("rate_per_kg")),
        "amount": _f(row.get("amount")),
        "uom": row.get("uom"),
        "case_pack": row.get("case_pack"),
        "net_weight": row.get("net_weight"),
        "total_weight": row.get("total_weight"),
        "batch_number": row.get("batch_number"),
        "lot_number": row.get("lot_number"),
        "manufacturing_date": row.get("manufacturing_date"),
        "expiry_date": row.get("expiry_date"),
        "line_remarks": row.get("line_remarks"),
        "cold_unit": row.get("cold_unit"),
        "item_mark": row.get("item_mark"),
        "box_id": row.get("box_id"),
        "transaction_no": row.get("transaction_no"),
        # The snapshot itself is deliberately not returned (it is a full
        # inventory row, and callers only need to know it was taken) — its
        # presence is the proof the box actually left cold stock.
        "cold_deducted": _json(row.get("cold_storage_snapshot")) is not None,
    }


def _map_header(row: dict) -> dict:
    out = {k: _s(row.get(k)) for k in (
        "job_work_date", "from_warehouse", "to_party", "party_address", "party_state",
        "party_city", "party_pin_code", "party_contact_company", "party_contact_mobile",
        "party_email", "sub_category", "contact_person", "contact_number",
        "purpose_of_work", "expected_return_date", "vehicle_no", "driver_name",
        "authorized_person", "remarks", "e_way_bill_no", "dispatched_through",
        "created_by", "created_at", "updated_at",
    )}
    out["id"] = row["id"]
    out["challan_no"] = row.get("challan_no") or ""
    out["type"] = row.get("type") or "OUT"
    out["status"] = row.get("status") or "sent"
    out["dispatch_to"] = _json(row.get("dispatch_to"))
    return out


async def get_job_work(conn, header_id: int) -> dict:
    """Header + its lines. 404s if the id is unknown."""
    row = await conn.fetchrow(
        f"SELECT {HEADER_COLS} FROM jb_materialout_header WHERE id = $1", header_id)
    if not row:
        raise HTTPException(
            404,
            detail={"error": "job_work_not_found",
                    "message": f"No job-work record {header_id}",
                    "details": {"id": header_id}},
        )
    lines = await conn.fetch(
        f"SELECT {LINE_COLS} FROM jb_materialout_lines WHERE header_id = $1 "
        "ORDER BY sl_no NULLS LAST, id",
        header_id,
    )
    record = _map_header(dict(row))
    record["lines"] = [_map_line(dict(r)) for r in lines]
    return record
