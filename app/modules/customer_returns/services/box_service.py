"""Customer-Returns box operations: single-box Print upsert, bulk box sync,
and the box-edit audit log. Boxes are keyed by (rtv_id, article_description,
box_number); box_id is NULL until Print and never regenerated once set.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import query_service as q
from app.modules.customer_returns.tables import cr_table_names

# Cold-storage warehouses whose returned boxes IMS mirrors into {prefix}_cold_stocks
# (rtv_tools._RTV_COLD_UNIT_MAP: Savla D-39 / D-514, Rishi, Supreme, Eskimo — NOT
# W-202). CFERP does NOT yet rebuild that mirror (Phase 4), so to avoid silently
# desyncing IMS cold inventory, cold-warehouse box entry is refused here and done in
# IMS. Matched on the raw stored factory_unit AND its canonical form, separator/case
# insensitive.
_COLD_UNITS = {"d39", "d514", "rishi", "supreme", "eskimo", "savlad39", "savlad514"}


def _is_cold_warehouse(factory_unit) -> bool:
    if not factory_unit:
        return False
    return re.sub(r"[^a-z0-9]", "", str(factory_unit).lower()) in _COLD_UNITS


async def _resolve_box_writable_header_id(conn, tables: dict, cr_id: str) -> int:
    """header_id for a box write; 404 if absent, 409 if it's a cold-storage return
    (those go through IMS so its cold_stocks mirror stays correct — see _COLD_UNITS)."""
    row = await conn.fetchrow(
        f"SELECT id, factory_unit FROM {tables['header']} WHERE rtv_id = $1", cr_id)
    if row is None:
        raise HTTPException(
            404,
            detail={"error": "customer_return_not_found",
                    "message": f"No customer return {cr_id}", "details": {"rtv_id": cr_id}},
        )
    if _is_cold_warehouse(row["factory_unit"]):
        raise HTTPException(
            409,
            detail={"error": "cold_warehouse_boxes_in_ims",
                    "message": "Cold-storage box entry is done in IMS so cold inventory "
                               "stays in sync. Enter and print boxes for this return in IMS.",
                    "details": {"rtv_id": cr_id, "factory_unit": row["factory_unit"]}},
        )
    return row["id"]


def _box_lock_key(company: str, cr_id: str) -> str:
    """Advisory-lock key serializing all box writes for one CR. Restores the
    atomicity the natural-key ON CONFLICT gave on the module's own tables — the
    legacy _rtv_boxes has no (header_id, article, box_number) unique constraint
    (adding one could reject a valid IMS row), so concurrent box writes for the
    same CR are serialized with pg_advisory_xact_lock instead."""
    return f"crbox:{company}:{cr_id}"


def _base8() -> str:
    """Last 8 digits of epoch-milliseconds — the box_id prefix."""
    return str(int(time.time() * 1000))[-8:]


def _gen_single_box_id(box_number: int) -> str:
    """Single-print box_id: '{base8}-{box_number}' (two parts)."""
    return f"{_base8()}-{box_number}"


async def _resolve_line_id(conn, tables: dict, header_id: int, article: str):
    """The legacy line id for a box's article (box.article_description ==
    line.item_description) — set on the box's rtv_line_id FK so IMS's box->line join
    holds. None when the article has no matching line (mirrors the orphan boxes IMS
    itself carries)."""
    return await conn.fetchval(
        f"SELECT id FROM {tables['lines']} WHERE header_id = $1 AND item_description = $2 LIMIT 1",
        header_id, article,
    )


async def upsert_box(conn, company: str, cr_id: str,
                     payload: schemas.CRBoxUpsertRequest) -> dict:
    """Print/print-edit a single box. A fresh box_id is minted; on an existing row
    the stored box_id is preserved so an already-printed box keeps its id and an
    unprinted one gets the new id. Every mutable field uses COALESCE(new, existing)
    so a None payload value never nulls a stored value. A CR-level advisory lock
    serializes concurrent box writes for the same CR (the legacy _rtv_boxes has no
    natural-key unique constraint to make an ON CONFLICT atomic)."""
    tables = cr_table_names(company)
    header_id = await _resolve_box_writable_header_id(conn, tables, cr_id)
    line_id = await _resolve_line_id(conn, tables, header_id, payload.article_description)

    new_box_id = _gen_single_box_id(payload.box_number)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1)::bigint)",
                           _box_lock_key(company, cr_id))
        existing = await conn.fetchrow(
            f"SELECT id, box_id FROM {tables['boxes']} "
            "WHERE header_id = $1 AND article_description = $2 AND box_number = $3",
            header_id, payload.article_description, payload.box_number,
        )
        if existing:
            box_id = existing["box_id"] or new_box_id
            await conn.execute(
                f"""
                UPDATE {tables['boxes']} SET
                    box_id = $2,
                    rtv_line_id = COALESCE(rtv_line_id, $3),
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
                WHERE id = $1
                """,
                existing["id"], box_id, line_id, payload.uom, payload.conversion,
                payload.net_weight, payload.gross_weight, payload.lot_number,
                payload.item_mark, payload.spl_remarks, payload.vakkal, payload.count,
            )
            status = "updated"
        else:
            box_id = new_box_id
            await conn.execute(
                f"""
                INSERT INTO {tables['boxes']}
                    (header_id, rtv_line_id, article_description, box_number, box_id, uom,
                     conversion, net_weight, gross_weight, lot_number, item_mark,
                     spl_remarks, vakkal, count)
                VALUES ($1,$2,$3,$4,$5,$6,$7,
                        COALESCE($8::numeric, 0), COALESCE($9::numeric, 0),
                        $10,$11,$12,$13,$14)
                """,
                header_id, line_id, payload.article_description, payload.box_number, box_id,
                payload.uom, payload.conversion, payload.net_weight, payload.gross_weight,
                payload.lot_number, payload.item_mark, payload.spl_remarks,
                payload.vakkal, payload.count,
            )
            status = "inserted"
    return {"status": status, "box_id": box_id, "rtv_id": cr_id,
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
    header_id = await _resolve_box_writable_header_id(conn, tables, cr_id)

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

    inserted = updated = deleted = 0
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1)::bigint)",
                           _box_lock_key(company, cr_id))
        # Read the existing box set + line map UNDER the lock (not before it) so the
        # insert-vs-update-vs-delete decision can't be made from a stale pre-lock
        # snapshot — _rtv_boxes has no natural-key unique constraint to catch a
        # duplicate INSERT, so a check-and-act split across the lock could persist a
        # duplicate (header_id, article, box_number) row and orphan a printed box_id.
        line_rows = await conn.fetch(
            f"SELECT id, item_description FROM {tables['lines']} WHERE header_id = $1", header_id)
        line_id_by_article = {r["item_description"]: r["id"] for r in line_rows}
        existing_rows = await conn.fetch(
            f"SELECT article_description, box_number FROM {tables['boxes']} WHERE header_id = $1",
            header_id)
        existing_keys = {(r["article_description"], r["box_number"]) for r in existing_rows}
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
                        rtv_line_id = COALESCE(rtv_line_id, $13),
                        updated_at = NOW()
                    WHERE header_id = $1 AND article_description = $2 AND box_number = $3
                    """,
                    header_id, art, num, b.uom, b.conversion, b.net_weight, b.gross_weight,
                    b.lot_number, b.item_mark, b.spl_remarks, b.vakkal, b.count,
                    line_id_by_article.get(art),
                )
                updated += 1
            else:
                # box_id stays NULL until the box is actually Printed (upsert_box) —
                # a bulk save is not a print. Minting one here would make the row read
                # back as "printed", freezing it in the UI though no label was produced.
                await conn.execute(
                    f"""
                    INSERT INTO {tables['boxes']}
                        (header_id, rtv_line_id, article_description, box_number, box_id, uom,
                         conversion, net_weight, gross_weight, lot_number, item_mark, spl_remarks, vakkal, count)
                    VALUES ($1,$2,$3,$4,NULL,$5,$6,
                            COALESCE($7::numeric, 0), COALESCE($8::numeric, 0),
                            $9,$10,$11,$12,$13)
                    """,
                    header_id, line_id_by_article.get(art), art, num, b.uom, b.conversion,
                    b.net_weight, b.gross_weight, b.lot_number, b.item_mark, b.spl_remarks,
                    b.vakkal, b.count,
                )
                inserted += 1

        for (art, num) in existing_keys - incoming_keys:
            await conn.execute(
                f"DELETE FROM {tables['boxes']} "
                "WHERE header_id = $1 AND article_description = $2 AND box_number = $3",
                header_id, art, num,
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
