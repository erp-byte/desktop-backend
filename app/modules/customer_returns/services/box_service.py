"""Customer-Returns box operations: single-box Print upsert, bulk box sync,
and the box-edit audit log. Boxes are keyed by (rtv_id, article_description,
box_number); box_id is NULL until Print and never regenerated once set.
"""
from __future__ import annotations

import time

from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.tables import cr_table_names


def _base8() -> str:
    """Last 8 digits of epoch-milliseconds — the box_id prefix."""
    return str(int(time.time() * 1000))[-8:]


def _gen_single_box_id(box_number: int) -> str:
    """Single-print box_id: '{base8}-{box_number}' (two parts)."""
    return f"{_base8()}-{box_number}"


async def _assert_cr_exists(conn, header_table: str, cr_id: str) -> None:
    exists = await conn.fetchval(f"SELECT 1 FROM {header_table} WHERE rtv_id = $1", cr_id)
    if not exists:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )


async def upsert_box(conn, company: str, cr_id: str,
                     payload: schemas.CRBoxUpsertRequest) -> dict:
    """Print/print-edit a single box. 3-way: existing+printed → COALESCE-update
    (preserve box_id); existing+unprinted → gen id + update; absent → insert."""
    tables = cr_table_names(company)
    await _assert_cr_exists(conn, tables["header"], cr_id)

    existing = await conn.fetchrow(
        f"SELECT box_id FROM {tables['boxes']} "
        "WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3",
        cr_id, payload.article_description, payload.box_number,
    )

    if existing is not None:
        box_id = existing["box_id"] or _gen_single_box_id(payload.box_number)
        async with conn.transaction():
            await conn.execute(
                f"""
                UPDATE {tables['boxes']} SET
                    box_id = $4,
                    uom = COALESCE($5, uom),
                    conversion = COALESCE($6, conversion),
                    net_weight = COALESCE($7::numeric, net_weight),
                    gross_weight = COALESCE($8::numeric, gross_weight),
                    lot_number = COALESCE($9, lot_number),
                    item_mark = COALESCE($10, item_mark),
                    spl_remarks = COALESCE($11, spl_remarks),
                    vakkal = COALESCE($12, vakkal),
                    count = COALESCE($13::int, count),
                    updated_at = NOW()
                WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3
                """,
                cr_id, payload.article_description, payload.box_number, box_id,
                payload.uom, payload.conversion, payload.net_weight, payload.gross_weight,
                payload.lot_number, payload.item_mark, payload.spl_remarks,
                payload.vakkal, payload.count,
            )
        status = "updated"
    else:
        box_id = _gen_single_box_id(payload.box_number)
        async with conn.transaction():
            await conn.execute(
                f"""
                INSERT INTO {tables['boxes']}
                    (rtv_id, article_description, box_number, box_id, uom, conversion,
                     net_weight, gross_weight, lot_number, item_mark, spl_remarks, vakkal, count)
                VALUES ($1,$2,$3,$4,$5,$6,
                        COALESCE($7::numeric, 0), COALESCE($8::numeric, 0),
                        $9,$10,$11,$12,$13)
                """,
                cr_id, payload.article_description, payload.box_number, box_id,
                payload.uom, payload.conversion, payload.net_weight, payload.gross_weight,
                payload.lot_number, payload.item_mark, payload.spl_remarks,
                payload.vakkal, payload.count,
            )
        status = "inserted"

    return {"status": status, "box_id": box_id, "rtv_id": cr_id,
            "article_description": payload.article_description, "box_number": payload.box_number}
