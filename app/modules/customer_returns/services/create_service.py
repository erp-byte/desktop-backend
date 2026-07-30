"""Customer-Returns write side: create/update/delete (header + lines).

Owns its own transaction (mirrors transfer/create_service). The rtv_id string is
the header PK; a same-second collision retries inside a SAVEPOINT with a numeric
suffix so the common id stays 'CR-YYYYMMDDHHMMSS'.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import query_service as q
from app.modules.customer_returns.tables import cr_table_names

_IST = ZoneInfo("Asia/Kolkata")


def _generate_cr_id() -> str:
    return "CR-" + datetime.now(_IST).strftime("%Y%m%d%H%M%S")


async def _insert_line(conn, tables: dict, header_id: int, line: schemas.CRLineCreate) -> None:
    # Legacy _rtv_lines is keyed by header_id + surrogate id (no rtv_id, no natural
    # unique constraint) — a plain INSERT. Callers always insert under a fresh header
    # (create) or after DELETE-ing the header's lines (update_cr_lines), so there is
    # never a live conflict to upsert against.
    qty = int(q._to_float(line.qty) or 0)
    rate = q._to_float(line.rate) or 0.0
    value = q._line_value(qty, rate, line.value)
    net_weight = q._to_float(line.net_weight) or 0.0
    carton_weight = q._to_float(line.carton_weight) or 0.0
    await conn.execute(
        f"""
        INSERT INTO {tables['lines']}
            (header_id, item_description, material_type, item_category, sub_category, sale_group,
             uom, qty, rate, value, conversion, net_weight, carton_weight,
             lot_number, item_mark, spl_remarks, vakkal)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
        """,
        header_id, line.item_description, line.material_type, line.item_category,
        line.sub_category, line.sale_group, line.uom, qty, rate, value, line.conversion,
        net_weight, carton_weight,
        line.lot_number, line.item_mark, line.spl_remarks, line.vakkal,
    )


async def _insert_header(conn, tables: dict, header: schemas.CRHeaderCreate,
                         created_by: str) -> tuple[str, int]:
    """Insert the header; return (rtv_id, surrogate id). The id is the FK lines/boxes
    hang off in the legacy _rtv_* schema. rtv_id is UNIQUE — a same-second collision
    retries with a numeric suffix inside a SAVEPOINT."""
    base = _generate_cr_id()
    conversion = q._to_float(header.conversion) or 0.0
    for attempt in range(6):
        cand = base if attempt == 0 else f"{base}-{attempt}"
        try:
            async with conn.transaction():  # SAVEPOINT — isolates rtv_id-collision retry
                row = await conn.fetchrow(
                    f"""
                    INSERT INTO {tables['header']}
                        (rtv_id, factory_unit, customer, invoice_number, challan_no, dn_no,
                         conversion, sales_poc, sales_poc_email, business_head, remark,
                         vehicle_number, transporter_name, driver_name, inward_manager,
                         status, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'Pending',$16)
                    RETURNING id
                    """,
                    cand, header.factory_unit, header.customer, header.invoice_number,
                    header.challan_no, header.dn_no, conversion, header.sales_poc,
                    header.sales_poc_email, header.business_head, header.remark,
                    header.vehicle_number, header.transporter_name, header.driver_name,
                    header.inward_manager, created_by,
                )
            return cand, row["id"]
        except asyncpg.UniqueViolationError:
            continue
    raise HTTPException(
        500,
        detail={"error": "cr_id_generation_failed",
                "message": "Could not allocate a unique CR id"},
    )


async def create_cr(conn, company: str, data: schemas.CRCreate, created_by: str) -> dict:
    if data.company.upper() != (company or "").strip().upper():
        raise HTTPException(
            400,
            detail={"error": "company_mismatch",
                    "message": "path company and body company differ",
                    "details": {"path": company, "body": data.company}},
        )
    tables = cr_table_names(company)
    async with conn.transaction():
        cr_id, header_id = await _insert_header(conn, tables, data.header, created_by)
        for line in data.lines:
            await _insert_line(conn, tables, header_id, line)
    # Read back AFTER commit so the response matches get/list exactly.
    return await q.get_cr(conn, company, cr_id)


_HEADER_UPDATABLE = [
    "factory_unit", "customer", "invoice_number", "challan_no", "dn_no", "conversion",
    "sales_poc", "sales_poc_email", "business_head", "remark", "status",
    "vehicle_number", "transporter_name", "driver_name", "inward_manager",
]


async def update_cr(conn, company: str, cr_id: str, data: schemas.CRHeaderUpdate) -> dict:
    tables = cr_table_names(company)
    provided = data.model_dump(exclude_none=True)
    if not provided:
        raise HTTPException(
            400,
            detail={"error": "empty_update", "message": "Provide at least one field to update"},
        )
    sets: list[str] = []
    args: list = []
    for col in _HEADER_UPDATABLE:
        if col in provided:
            val = provided[col]
            if col == "conversion":
                val = q._to_float(val) or 0.0
            args.append(val); sets.append(f"{col} = ${len(args)}")
    sets.append("updated_at = NOW()")
    args.append(cr_id)
    row = await conn.fetchrow(
        f"UPDATE {tables['header']} SET {', '.join(sets)} WHERE rtv_id = ${len(args)} "
        f"RETURNING {q.HEADER_COLS}",
        *args,
    )
    if not row:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    return q._map_header_row(dict(row))


async def update_cr_lines(conn, company: str, cr_id: str,
                          data: schemas.CRLinesUpdateRequest) -> dict:
    tables = cr_table_names(company)
    header_id = await q.resolve_header_id(conn, tables, cr_id)
    lines_count = len(data.lines)  # rows actually inserted (duplicate descriptions allowed)
    async with conn.transaction():
        await conn.execute(f"DELETE FROM {tables['lines']} WHERE header_id = $1", header_id)
        for line in data.lines:
            await _insert_line(conn, tables, header_id, line)
        # DELETE-ing the lines nulled every box's rtv_line_id (FK ON DELETE SET NULL);
        # re-point each box to its re-created line so IMS's box->line join survives.
        await conn.execute(
            f"""
            UPDATE {tables['boxes']} b SET rtv_line_id = l.id
              FROM {tables['lines']} l
             WHERE b.header_id = $1 AND l.header_id = $1
               AND b.article_description = l.item_description
            """,
            header_id,
        )
    return {"status": "updated", "rtv_id": cr_id, "lines_count": lines_count}


async def delete_cr(conn, company: str, cr_id: str) -> dict:
    tables = cr_table_names(company)
    header_id = await q.resolve_header_id(conn, tables, cr_id)
    lines_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['lines']} WHERE header_id = $1", header_id)
    boxes_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['boxes']} WHERE header_id = $1", header_id)
    async with conn.transaction():
        # FK ON DELETE CASCADE (lines.header_id / boxes.header_id) removes them with the header.
        await conn.execute(f"DELETE FROM {tables['header']} WHERE rtv_id = $1", cr_id)
    return {"success": True, "message": f"Customer return {cr_id} deleted",
            "rtv_id": cr_id, "lines_count": int(lines_count or 0),
            "boxes_count": int(boxes_count or 0)}
