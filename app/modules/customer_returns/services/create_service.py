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


async def _insert_line(conn, tables: dict, cr_id: str, line: schemas.CRLineCreate) -> None:
    qty = int(q._to_float(line.qty) or 0)
    rate = q._to_float(line.rate) or 0.0
    value = q._line_value(qty, rate, line.value)
    net_weight = q._to_float(line.net_weight) or 0.0
    carton_weight = q._to_float(line.carton_weight) or 0.0
    await conn.execute(
        f"""
        INSERT INTO {tables['lines']}
            (rtv_id, item_description, material_type, item_category, sub_category, uom,
             qty, rate, value, net_weight, carton_weight, lot_number, item_mark, spl_remarks, vakkal)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        ON CONFLICT (rtv_id, item_description) DO UPDATE SET
            material_type=EXCLUDED.material_type, item_category=EXCLUDED.item_category,
            sub_category=EXCLUDED.sub_category, uom=EXCLUDED.uom, qty=EXCLUDED.qty,
            rate=EXCLUDED.rate, value=EXCLUDED.value, net_weight=EXCLUDED.net_weight,
            carton_weight=EXCLUDED.carton_weight, lot_number=EXCLUDED.lot_number,
            item_mark=EXCLUDED.item_mark, spl_remarks=EXCLUDED.spl_remarks,
            vakkal=EXCLUDED.vakkal, updated_at=NOW()
        """,
        cr_id, line.item_description, line.material_type, line.item_category,
        line.sub_category, line.uom, qty, rate, value, net_weight, carton_weight,
        line.lot_number, line.item_mark, line.spl_remarks, line.vakkal,
    )


async def _insert_header(conn, tables: dict, header: schemas.CRHeaderCreate,
                         created_by: str) -> str:
    base = _generate_cr_id()
    conversion = q._to_float(header.conversion) or 0.0
    for attempt in range(6):
        cand = base if attempt == 0 else f"{base}-{attempt}"
        try:
            async with conn.transaction():  # SAVEPOINT — isolates PK-collision retry
                await conn.execute(
                    f"""
                    INSERT INTO {tables['header']}
                        (rtv_id, factory_unit, customer, invoice_number, challan_no, dn_no,
                         conversion, sales_poc, sales_poc_email, business_head, remark,
                         vehicle_number, transporter_name, driver_name, inward_manager,
                         status, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'Pending',$16)
                    """,
                    cand, header.factory_unit, header.customer, header.invoice_number,
                    header.challan_no, header.dn_no, conversion, header.sales_poc,
                    header.sales_poc_email, header.business_head, header.remark,
                    header.vehicle_number, header.transporter_name, header.driver_name,
                    header.inward_manager, created_by,
                )
            return cand
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
        cr_id = await _insert_header(conn, tables, data.header, created_by)
        for line in data.lines:
            await _insert_line(conn, tables, cr_id, line)
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
    exists = await conn.fetchval(
        f"SELECT 1 FROM {tables['header']} WHERE rtv_id = $1", cr_id
    )
    if not exists:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    lines_count = len({l.item_description for l in data.lines})
    async with conn.transaction():
        await conn.execute(f"DELETE FROM {tables['lines']} WHERE rtv_id = $1", cr_id)
        for line in data.lines:
            await _insert_line(conn, tables, cr_id, line)
    return {"status": "updated", "rtv_id": cr_id, "lines_count": lines_count}


async def delete_cr(conn, company: str, cr_id: str) -> dict:
    tables = cr_table_names(company)
    hdr = await conn.fetchrow(
        f"SELECT rtv_id FROM {tables['header']} WHERE rtv_id = $1", cr_id
    )
    if not hdr:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    lines_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['lines']} WHERE rtv_id = $1", cr_id)
    boxes_count = await conn.fetchval(
        f"SELECT COUNT(*) FROM {tables['boxes']} WHERE rtv_id = $1", cr_id)
    async with conn.transaction():
        # FK ON DELETE CASCADE removes lines/boxes with the header.
        await conn.execute(f"DELETE FROM {tables['header']} WHERE rtv_id = $1", cr_id)
    return {"success": True, "message": f"Customer return {cr_id} deleted",
            "rtv_id": cr_id, "lines_count": int(lines_count or 0),
            "boxes_count": int(boxes_count or 0)}
