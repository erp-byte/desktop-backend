"""Create a new transfer request (doc 05).

Ported from `transfer_backend_reference/.../interunit_tools.py::create_request`
(sync SQLAlchemy → asyncpg). Inserts a header row into
`interunit_transfer_requests` with status 'Pending' plus one
`interunit_transfer_request_lines` row per article, inside a single
transaction. Reuses the query_service row mappers so the response envelope
matches the list/get endpoints (RequestWithLines)."""
from __future__ import annotations

from datetime import datetime

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.transfer import schemas
from app.modules.transfer.services.query_service import (
    _convert_date,
    _map_request_header,
    _map_request_line,
)


# DB chk_uom constraint on interunit_transfer_request_lines. Anything outside
# this set (e.g. the form's legacy "BAG") is stored as NULL rather than raising
# a CheckViolation 500 — NULL satisfies the CHECK.
_ALLOWED_UOM = {"KG", "PCS", "BOX", "CARTON"}


def _generate_request_no() -> str:
    """Fallback request number (minute precision) — mirrors reference."""
    return "REQ" + datetime.now().strftime("%Y%m%d%H%M")


def _uom(v) -> str | None:
    u = (v or "").upper().strip()
    return u if u in _ALLOWED_UOM else None


def _f(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _i(v) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


async def create_request(conn, data: schemas.RequestCreate, created_by: str) -> dict:
    request_date = _convert_date(data.form_data.request_date)
    request_no = (
        data.computed_fields.request_no
        if data.computed_fields and data.computed_fields.request_no
        else _generate_request_no()
    )

    async with conn.transaction():
        async def _ins_request():
            return await conn.fetchrow(
                """
                INSERT INTO interunit_transfer_requests
                    (request_no, request_date, from_site, to_site,
                     reason_code, remarks, status, created_by, created_ts, id)
                VALUES ($1, $2, $3, $4, $5, $6, 'Pending', $7, $8, $9)
                RETURNING id, request_no, request_date, from_site, to_site,
                          reason_code, remarks, status, reject_reason,
                          created_by, created_ts, rejected_ts, updated_at
                """,
                request_no,
                request_date,
                data.form_data.from_warehouse,
                data.form_data.to_warehouse,
                data.form_data.reason_description or "General Transfer",
                data.form_data.reason_description or "No remarks",
                created_by,
                datetime.now(),
                new_short_time_id(),
            )
        header = await insert_with_pk_retry(conn, _ins_request)
        request_id = header["id"]

        lines = []
        for line in data.article_data:
            pack_size_f = _f(line.pack_size)
            qty_i = _i(line.quantity)
            unit_pack = _f(line.unit_pack_size)

            fe_net = _f(line.net_weight)
            if fe_net > 0:
                net_weight = round(fe_net, 3)
            elif (line.material_type or "").upper() == "FG":
                net_weight = round(unit_pack * pack_size_f * qty_i, 3)
            else:
                net_weight = round(pack_size_f * qty_i, 3)

            fe_total = _f(line.total_weight)
            total_weight = round(fe_total, 3) if fe_total > 0 else net_weight

            async def _ins_request_line(line=line, pack_size_f=pack_size_f, qty_i=qty_i,
                                        unit_pack=unit_pack, net_weight=net_weight,
                                        total_weight=total_weight):
                return await conn.fetchrow(
                    """
                    INSERT INTO interunit_transfer_request_lines
                        (request_id, rm_pm_fg_type, item_category, sub_category,
                         item_desc_raw, pack_size, qty, uom,
                         unit_pack_size, net_weight, total_weight, lot_number, id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    RETURNING id, request_id, rm_pm_fg_type, item_category, sub_category,
                              item_desc_raw, pack_size, qty, uom,
                              unit_pack_size, net_weight, total_weight, lot_number,
                              created_at, updated_at
                    """,
                    request_id,
                    line.material_type,
                    line.item_category,
                    line.sub_category,
                    line.item_description,
                    pack_size_f,
                    qty_i,
                    _uom(line.uom),
                    unit_pack,
                    net_weight,
                    total_weight,
                    line.lot_number,
                    new_short_time_id(),
                )
            row = await insert_with_pk_retry(conn, _ins_request_line)
            lines.append(_map_request_line(dict(row)))

    result = _map_request_header(dict(header))
    result["lines"] = lines
    return result
