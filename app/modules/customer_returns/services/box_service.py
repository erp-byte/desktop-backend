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
    """Print/print-edit a single box, atomically. A fresh box_id is minted; on
    conflict the existing box_id is preserved (COALESCE(existing, new)) so an
    already-printed box keeps its id and an unprinted one gets the new id. Every
    mutable field uses COALESCE(new, existing) so a None payload value never
    nulls a stored value. Atomic upsert avoids the double-print race."""
    tables = cr_table_names(company)
    await _assert_cr_exists(conn, tables["header"], cr_id)

    new_box_id = _gen_single_box_id(payload.box_number)
    async with conn.transaction():
        row = await conn.fetchrow(
            f"""
            INSERT INTO {tables['boxes']}
                (rtv_id, article_description, box_number, box_id, uom, conversion,
                 net_weight, gross_weight, lot_number, item_mark, spl_remarks, vakkal, count)
            VALUES ($1,$2,$3,$4,$5,$6,
                    COALESCE($7::numeric, 0), COALESCE($8::numeric, 0),
                    $9,$10,$11,$12,$13)
            ON CONFLICT (rtv_id, article_description, box_number) DO UPDATE SET
                box_id = COALESCE({tables['boxes']}.box_id, EXCLUDED.box_id),
                uom = COALESCE($5, {tables['boxes']}.uom),
                conversion = COALESCE($6, {tables['boxes']}.conversion),
                net_weight = COALESCE($7::numeric, {tables['boxes']}.net_weight),
                gross_weight = COALESCE($8::numeric, {tables['boxes']}.gross_weight),
                lot_number = COALESCE($9, {tables['boxes']}.lot_number),
                item_mark = COALESCE($10, {tables['boxes']}.item_mark),
                spl_remarks = COALESCE($11, {tables['boxes']}.spl_remarks),
                vakkal = COALESCE($12, {tables['boxes']}.vakkal),
                count = COALESCE($13::int, {tables['boxes']}.count),
                updated_at = NOW()
            RETURNING (xmax = 0) AS inserted, box_id
            """,
            cr_id, payload.article_description, payload.box_number, new_box_id,
            payload.uom, payload.conversion, payload.net_weight, payload.gross_weight,
            payload.lot_number, payload.item_mark, payload.spl_remarks,
            payload.vakkal, payload.count,
        )
    status = "inserted" if row["inserted"] else "updated"
    return {"status": status, "box_id": row["box_id"], "rtv_id": cr_id,
            "article_description": payload.article_description, "box_number": payload.box_number}
