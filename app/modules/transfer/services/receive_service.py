"""Transfer-IN receive lifecycle (pending header + box acknowledgement).

Ported from interunit_tools.py (create_pending_transfer_in, acknowledge_pending_box,
acknowledge_pending_boxes_batch, unacknowledge_pending_box, get_pending_by_transfer_out)
to asyncpg.

P8b-1 scope = record-only writes: these touch ONLY interunit_transfer_in_header /
interunit_transfer_in_boxes and are fully reversible. They never move source or
destination inventory. The STBR reconciliation block (which remaps
pending_transfer_stock on a box-id mismatch) and finalize/pick_from_pending (which
posts stock to cold_stocks) are deferred to P8b-2 — see the STBR note below.
"""
from __future__ import annotations

import json

from fastapi import HTTPException

from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.transfer.services import reversal_service, stock_service
from app.modules.transfer.services.query_service import (
    _fetch_transfer_in_boxes_q,
    _map_transfer_in_box,
    _map_transfer_in_header,
)

_HEADER_COLS = """
    id, transfer_out_id, transfer_out_no, grn_number, grn_date,
    receiving_warehouse, received_by, received_at,
    box_condition, condition_remarks, status, created_at, updated_at
"""


async def _header_with_boxes(conn, header_id: int) -> dict:
    row = await conn.fetchrow(
        f"SELECT {_HEADER_COLS} FROM interunit_transfer_in_header WHERE id = $1", header_id)
    result = _map_transfer_in_header(dict(row))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def create_pending_transfer_in(conn, data) -> dict:
    """Create a Pending transfer-in header. Idempotent: returns the existing
    pending header if one already exists for the transfer-out."""
    transfer_out = await conn.fetchrow(
        "SELECT id, challan_no FROM interunit_transfers_header WHERE id = $1", data.transfer_out_id)
    if not transfer_out:
        raise HTTPException(404, "Transfer OUT not found")

    existing_in = await conn.fetchrow(
        "SELECT id, status FROM interunit_transfer_in_header WHERE transfer_out_id = $1",
        data.transfer_out_id)
    if existing_in:
        if existing_in["status"] == "Pending":
            return await _header_with_boxes(conn, existing_in["id"])
        raise HTTPException(400, "Transfer OUT already has a completed Transfer IN (GRN) record")

    existing_grn = await conn.fetchrow(
        "SELECT id FROM interunit_transfer_in_header WHERE grn_number = $1", data.grn_number)
    if existing_grn:
        raise HTTPException(400, f"GRN number {data.grn_number} already exists")

    async def _ins_in_header():
        return await conn.fetchrow(
            f"""
            INSERT INTO interunit_transfer_in_header
                (transfer_out_id, transfer_out_no, grn_number, grn_date,
                 receiving_warehouse, received_by, received_at,
                 box_condition, condition_remarks, status, id)
            VALUES
                ($1, $2, $3, CURRENT_TIMESTAMP, $4, $5, CURRENT_TIMESTAMP, $6, $7, 'Pending', $8)
            RETURNING {_HEADER_COLS}
            """,
            data.transfer_out_id, transfer_out["challan_no"], data.grn_number,
            data.receiving_warehouse, data.received_by,
            data.box_condition, data.condition_remarks, new_short_time_id(),
        )
    async with conn.transaction():
        header = await insert_with_pk_retry(conn, _ins_in_header)

    result = _map_transfer_in_header(dict(header))
    result["boxes"] = []
    result["total_boxes_scanned"] = 0
    return result


async def acknowledge_pending_box(conn, header_id: int, data) -> dict:
    """UPSERT a single box/article into a pending transfer-in (record-only).

    NOTE: STBR (Scan-Time Box-ID Reconciliation) is intentionally NOT run here in
    P8b-1 — it remaps pending_transfer_stock (moves inventory) on a box-id swap and
    belongs with the stock-posting core in P8b-2. Without it, mismatched box-ids are
    simply recorded as scanned; the happy path (matched boxes) is unaffected.
    """
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id FROM interunit_transfer_in_header WHERE id = $1",
        header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    if header["status"] != "Pending":
        raise HTTPException(400, "Transfer IN is not in Pending status")

    issue_json = json.dumps(data.issue) if data.issue else None

    async def _ins_in_box():
        return await conn.fetchrow(
            """
            INSERT INTO interunit_transfer_in_boxes
                (header_id, box_id, article, batch_number, lot_number,
                 transaction_no, net_weight, gross_weight,
                 scanned_at, is_matched, transfer_out_box_id, issue, line_index, scan_source, id)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (header_id, box_id) DO UPDATE SET
                article = EXCLUDED.article,
                batch_number = EXCLUDED.batch_number,
                lot_number = EXCLUDED.lot_number,
                transaction_no = EXCLUDED.transaction_no,
                net_weight = EXCLUDED.net_weight,
                gross_weight = EXCLUDED.gross_weight,
                is_matched = EXCLUDED.is_matched,
                transfer_out_box_id = EXCLUDED.transfer_out_box_id,
                issue = EXCLUDED.issue,
                line_index = EXCLUDED.line_index,
                scanned_at = CURRENT_TIMESTAMP,
                scan_source = EXCLUDED.scan_source
            RETURNING id, header_id, box_id, article, batch_number,
                      lot_number, transaction_no, net_weight, gross_weight,
                      scanned_at, is_matched, transfer_out_box_id, issue, line_index
            """,
            header_id, data.box_id, data.article, data.batch_number, data.lot_number,
            data.transaction_no, data.net_weight, data.gross_weight,
            data.is_matched, data.transfer_out_box_id, issue_json, data.line_index,
            getattr(data, "scan_source", None) or "manual", new_short_time_id(),
        )
    async with conn.transaction():
        row = await insert_with_pk_retry(conn, _ins_in_box)

    result = _map_transfer_in_box(dict(row))
    # STBR deferred (P8b-2) — report a no-op so the frontend contract holds.
    result["reconciliation"] = {
        "status": "noop", "original_box_id": None,
        "propagated_count": 0, "siblings": [], "reconciliation_id": None,
    }
    return result


async def acknowledge_pending_boxes_batch(conn, header_id: int, boxes: list) -> dict:
    """Batch acknowledge — per-box, surfacing per-row failures rather than
    failing the whole batch (mirrors the reference)."""
    results, conflicts = [], []
    for box in boxes:
        try:
            results.append(await acknowledge_pending_box(conn, header_id, box))
        except HTTPException as e:
            conflicts.append({
                "box_id": getattr(box, "box_id", None),
                "transaction_no": getattr(box, "transaction_no", None),
                "status_code": e.status_code,
                "detail": e.detail,
            })
    return {"success": len(conflicts) == 0, "count": len(results),
            "boxes": results, "conflicts": conflicts}


async def unacknowledge_pending_box(conn, header_id: int, box_id: str) -> dict:
    header = await conn.fetchrow(
        "SELECT id, status FROM interunit_transfer_in_header WHERE id = $1", header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    if header["status"] != "Pending":
        raise HTTPException(400, "Transfer IN is not in Pending status")

    deleted = await conn.fetchrow(
        "DELETE FROM interunit_transfer_in_boxes WHERE header_id = $1 AND box_id = $2 RETURNING id",
        header_id, box_id)
    if not deleted:
        raise HTTPException(404, f"Box {box_id} not found in this transfer-in")
    return {"success": True, "deleted_box_id": box_id}


async def finalize_transfer_in(conn, header_id: int, data) -> dict:
    """Finalize a Pending transfer-in → Received. IRREVERSIBLE: posts stock via
    pick_from_pending (cold_stocks insert + pending delete) and flips the
    transfer-out to Received. All effects run in one transaction."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id, transfer_out_no FROM interunit_transfer_in_header WHERE id = $1",
        header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    if header["status"] != "Pending":
        raise HTTPException(400, "Transfer IN is not in Pending status")

    box_count = await conn.fetchval(
        "SELECT COUNT(*) FROM interunit_transfer_in_boxes WHERE header_id = $1", header_id)
    if not box_count:
        raise HTTPException(400, "No boxes/articles acknowledged. Cannot finalize.")

    async with conn.transaction():
        updated = await conn.fetchrow(
            f"""
            UPDATE interunit_transfer_in_header
            SET status = 'Received', received_at = CURRENT_TIMESTAMP,
                box_condition = $2, condition_remarks = $3, updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            RETURNING {_HEADER_COLS}
            """,
            header_id, data.box_condition, data.condition_remarks)

        picked = await stock_service.pick_from_pending(conn, header["transfer_out_id"])

        # Legacy fallback: transfers dispatched before pending_transfer_stock existed.
        if picked == 0 and getattr(data, "cold_storage_items", None):
            tout = await conn.fetchrow(
                "SELECT to_site FROM interunit_transfers_header WHERE id = $1", header["transfer_out_id"])
            await stock_service.insert_cold_storage_items(
                conn, header_id, data.cold_storage_items, header["transfer_out_no"],
                to_site=tout["to_site"] if tout else None)

        await conn.execute(
            "UPDATE interunit_transfers_header SET status = 'Received' WHERE id = $1",
            header["transfer_out_id"])

    result = _map_transfer_in_header(dict(updated))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def reopen_transfer_in(conn, header_id: int, reopened_by: str) -> dict:
    """Re-open a Received transfer-in → Pending. Reverses the posted stock back to
    in-transit (unpick_to_pending) and flips both headers, KEEPING the GRN and its
    acknowledged boxes so the operator can correct lots / raise issues. One txn."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id FROM interunit_transfer_in_header WHERE id = $1", header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    if header["status"] != "Received":
        raise HTTPException(400, "Only a Received transfer-in can be re-opened")

    async with conn.transaction():
        await reversal_service.unpick_to_pending(conn, header_id, header["transfer_out_id"])
        updated = await conn.fetchrow(
            f"""
            UPDATE interunit_transfer_in_header
            SET status = 'Pending', updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            RETURNING {_HEADER_COLS}
            """,
            header_id)
        await conn.execute(
            "UPDATE interunit_transfers_header SET status = 'Dispatch' WHERE id = $1",
            header["transfer_out_id"])

    result = _map_transfer_in_header(dict(updated))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def close_with_shortage(conn, header_id: int, shortage_reason: str | None, closed_by: str) -> dict:
    """Close a Pending transfer-in as Received WITH A SHORTAGE: post only the
    acknowledged boxes to the destination, then write off the un-received in-transit
    rows (they left the source at dispatch but never arrived). One txn."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id, condition_remarks FROM interunit_transfer_in_header WHERE id = $1",
        header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    if header["status"] != "Pending":
        raise HTTPException(400, "Transfer IN is not in Pending status")

    ack_rows = await conn.fetch(
        "SELECT box_id FROM interunit_transfer_in_boxes "
        "WHERE header_id = $1 AND box_id IS NOT NULL AND box_id <> ''", header_id)
    ack_ids = [r["box_id"] for r in ack_rows]
    if not ack_ids:
        raise HTTPException(400, "Acknowledge at least one box before closing with shortage.")

    async with conn.transaction():
        await stock_service.pick_from_pending(conn, header["transfer_out_id"], box_ids=set(ack_ids))
        # Write off the boxes that never arrived (the remaining In-Transit rows).
        written = await conn.fetchval(
            "WITH d AS (DELETE FROM pending_transfer_stock "
            "WHERE transfer_out_id = $1 AND status = 'In Transit' RETURNING 1) SELECT COUNT(*) FROM d",
            header["transfer_out_id"]) or 0
        note = f"Closed with shortage: {written} box(es) written off."
        if shortage_reason:
            note += f" Reason: {shortage_reason}"
        existing = (header["condition_remarks"] or "").strip()
        combined = f"{existing} | {note}" if existing else note
        updated = await conn.fetchrow(
            f"""
            UPDATE interunit_transfer_in_header
            SET status = 'Received', box_condition = 'Partial', received_at = CURRENT_TIMESTAMP,
                condition_remarks = $2, updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            RETURNING {_HEADER_COLS}
            """,
            header_id, combined)
        await conn.execute(
            "UPDATE interunit_transfers_header SET status = 'Received' WHERE id = $1",
            header["transfer_out_id"])

    result = _map_transfer_in_header(dict(updated))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    result["shortage_written_off"] = int(written)
    return result


async def edit_transfer_in(conn, header_id: int, data, edited_by: str) -> dict:
    """Edit a Received transfer-in. HEADER-only edits (grn / receiving warehouse /
    condition / remarks) update in place — no stock impact. PER-BOX edits (lot /
    weight / article) go through reverse → edit pending+boxes → re-finalize so the
    posted destination stock reflects the corrections. One transaction."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id FROM interunit_transfer_in_header WHERE id = $1", header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    if header["status"] != "Received":
        raise HTTPException(400, "Only a Received transfer-in can be edited")
    tout_id = header["transfer_out_id"]
    box_edits = data.boxes or []

    async with conn.transaction():
        if box_edits:
            # Reverse posted stock → in-transit, correct the boxes + pending rows, re-post.
            await reversal_service.unpick_to_pending(conn, header_id, tout_id)
            await conn.execute(
                "UPDATE interunit_transfer_in_header SET status = 'Pending' WHERE id = $1", header_id)
            for b in box_edits:
                net = stock_service._dec(b.net_weight)
                gross = stock_service._dec(b.gross_weight)
                await conn.execute(
                    """
                    UPDATE interunit_transfer_in_boxes
                    SET lot_number = COALESCE($3, lot_number), article = COALESCE($4, article),
                        net_weight = COALESCE($5, net_weight), gross_weight = COALESCE($6, gross_weight),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE header_id = $1 AND box_id = $2
                    """,
                    header_id, b.box_id, b.lot_number, b.article, net, gross)
                await conn.execute(
                    """
                    UPDATE pending_transfer_stock
                    SET lot_no = COALESCE($3, lot_no), item_description = COALESCE($4, item_description),
                        net_weight = COALESCE($5, net_weight), gross_weight = COALESCE($6, gross_weight),
                        weight_kg = COALESCE($5, weight_kg),
                        cold_storage_data = CASE
                            WHEN $5 IS NOT NULL AND cold_storage_data IS NOT NULL
                            THEN jsonb_set(cold_storage_data, '{total_inventory_kgs}', to_jsonb($5::numeric))
                            ELSE cold_storage_data END
                    WHERE transfer_out_id = $1 AND box_id = $2 AND status = 'In Transit'
                    """,
                    tout_id, b.box_id, b.lot_number, b.article, net, gross)
            await stock_service.pick_from_pending(conn, tout_id)
            updated = await conn.fetchrow(
                f"""
                UPDATE interunit_transfer_in_header
                SET status = 'Received',
                    grn_number = COALESCE($2, grn_number),
                    receiving_warehouse = COALESCE($3, receiving_warehouse),
                    box_condition = COALESCE($4, box_condition),
                    condition_remarks = COALESCE($5, condition_remarks),
                    received_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                RETURNING {_HEADER_COLS}
                """,
                header_id, data.grn_number, data.receiving_warehouse, data.box_condition, data.condition_remarks)
            await conn.execute(
                "UPDATE interunit_transfers_header SET status = 'Received' WHERE id = $1", tout_id)
        else:
            # Header-only edit — no stock impact, stays Received.
            updated = await conn.fetchrow(
                f"""
                UPDATE interunit_transfer_in_header
                SET grn_number = COALESCE($2, grn_number),
                    receiving_warehouse = COALESCE($3, receiving_warehouse),
                    box_condition = COALESCE($4, box_condition),
                    condition_remarks = COALESCE($5, condition_remarks),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                RETURNING {_HEADER_COLS}
                """,
                header_id, data.grn_number, data.receiving_warehouse, data.box_condition, data.condition_remarks)

    result = _map_transfer_in_header(dict(updated))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def create_transfer_in(conn, data) -> dict:
    # Fallback bulk receipt (no pending header) — also posts stock via
    # pick_from_pending + inserts scanned boxes. Less-used than the
    # pending→acknowledge→finalize path; deferred until its input schema lands.
    raise HTTPException(501, "Bulk transfer-in create is not yet wired (use the pending → acknowledge → finalize flow)")


async def get_pending_by_transfer_out(conn, transfer_out_id: int) -> dict:
    """Latest transfer-in header (+boxes) for a transfer-out. The caller RESUMES it
    when status is 'Pending', or offers RE-OPEN when 'Received' (gating adoption of
    its acknowledged boxes on the Pending status)."""
    row = await conn.fetchrow(
        f"""
        SELECT {_HEADER_COLS}
        FROM interunit_transfer_in_header
        WHERE transfer_out_id = $1
        ORDER BY id DESC
        LIMIT 1
        """,
        transfer_out_id)
    if not row:
        return {"exists": False, "header": None}
    header = await _header_with_boxes(conn, row["id"])
    return {"exists": True, "header": header}
