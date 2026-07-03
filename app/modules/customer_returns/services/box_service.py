"""Customer-Returns box operations: single-box Print upsert, bulk box sync,
and the box-edit audit log. Boxes are keyed by (rtv_id, article_description,
box_number); box_id is NULL until Print and never regenerated once set.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

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


async def bulk_save_boxes(conn, company: str, cr_id: str,
                          data: schemas.CRBulkBoxUpdateRequest,
                          notify_discrepancy: bool = True,
                          allow_clear: bool = False) -> dict:
    """State-aware full sync of the CR's box set: insert new, update existing
    (preserving box_id), delete boxes no longer present. Flips header status to
    'Submitted' ONLY from Approved/Submitted. `notify_discrepancy` is a reserved
    no-op (kept for signature parity). Cold-stock mirror is wired in Phase 4."""
    tables = cr_table_names(company)
    await _assert_cr_exists(conn, tables["header"], cr_id)

    if not data.boxes and not allow_clear:
        raise HTTPException(
            400,
            detail={"error": "empty_box_sync",
                    "message": "Refusing to delete all boxes for this return; "
                               "pass allow_clear=true to intentionally clear them.",
                    "details": {"rtv_id": cr_id}},
        )

    # dedupe incoming by (article, box_number), keep last occurrence
    seen: dict = {}
    for b in data.boxes:
        seen[(b.article_description, b.box_number)] = b
    incoming = seen  # key -> item, insertion order preserved
    incoming_keys = set(incoming.keys())

    existing_rows = await conn.fetch(
        f"SELECT article_description, box_number, box_id FROM {tables['boxes']} WHERE rtv_id = $1",
        cr_id,
    )
    existing_keys = {(r["article_description"], r["box_number"]) for r in existing_rows}

    inserted = updated = deleted = 0
    async with conn.transaction():
        for (art, num), b in incoming.items():
            if (art, num) in existing_keys:
                await conn.execute(
                    f"""
                    UPDATE {tables['boxes']} SET
                        uom = COALESCE($4, uom),
                        conversion = COALESCE($5, conversion),
                        net_weight = COALESCE($6::numeric, net_weight),
                        gross_weight = COALESCE($7::numeric, gross_weight),
                        lot_number = COALESCE($8, lot_number),
                        item_mark = COALESCE($9, item_mark),
                        spl_remarks = COALESCE($10, spl_remarks),
                        vakkal = COALESCE($11, vakkal),
                        count = COALESCE($12::int, count),
                        updated_at = NOW()
                    WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3
                    """,
                    cr_id, art, num, b.uom, b.conversion, b.net_weight, b.gross_weight,
                    b.lot_number, b.item_mark, b.spl_remarks, b.vakkal, b.count,
                )
                updated += 1
            else:
                box_id = f"{_base8()}-{num}-{inserted}"
                await conn.execute(
                    f"""
                    INSERT INTO {tables['boxes']}
                        (rtv_id, article_description, box_number, box_id, uom, conversion,
                         net_weight, gross_weight, lot_number, item_mark, spl_remarks, vakkal, count)
                    VALUES ($1,$2,$3,$4,$5,$6,
                            COALESCE($7::numeric, 0), COALESCE($8::numeric, 0),
                            $9,$10,$11,$12,$13)
                    """,
                    cr_id, art, num, box_id, b.uom, b.conversion, b.net_weight, b.gross_weight,
                    b.lot_number, b.item_mark, b.spl_remarks, b.vakkal, b.count,
                )
                inserted += 1

        for (art, num) in existing_keys - incoming_keys:
            await conn.execute(
                f"DELETE FROM {tables['boxes']} "
                "WHERE rtv_id = $1 AND article_description = $2 AND box_number = $3",
                cr_id, art, num,
            )
            deleted += 1

        await conn.execute(
            f"UPDATE {tables['header']} SET status = 'Submitted', updated_at = NOW() "
            "WHERE rtv_id = $1 AND status IN ('Approved', 'Submitted')",
            cr_id,
        )

    return {"status": "synced", "rtv_id": cr_id, "inserted": inserted,
            "updated": updated, "unchanged": 0, "deleted": deleted}


async def log_box_edits(conn, payload: schemas.CRBoxEditLogRequest, email_id: str) -> dict:
    """Append one audit row per change to the global box_edit_logs table.
    `email_id` is the JWT actor (payload.email_id is ignored — hardening).
    No CR/box existence check (append-only log, matches source)."""
    edited_at = datetime.now(timezone.utc)  # one shared timestamp per call
    async with conn.transaction():
        for ch in payload.changes:
            description = f"Changed {ch.field_name} from '{ch.old_value}' to '{ch.new_value}'"
            await conn.execute(
                """
                INSERT INTO box_edit_logs
                    (email_id, description, transaction_no, box_id, field_name,
                     old_value, new_value, edited_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                email_id, description, payload.rtv_id, payload.box_id,
                ch.field_name, ch.old_value, ch.new_value, edited_at,
            )
    return {"status": "logged", "entries": len(payload.changes)}
