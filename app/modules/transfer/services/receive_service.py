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

from app.modules.transfer.services import stock_service
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

    async with conn.transaction():
        header = await conn.fetchrow(
            f"""
            INSERT INTO interunit_transfer_in_header
                (transfer_out_id, transfer_out_no, grn_number, grn_date,
                 receiving_warehouse, received_by, received_at,
                 box_condition, condition_remarks, status)
            VALUES
                ($1, $2, $3, CURRENT_TIMESTAMP, $4, $5, CURRENT_TIMESTAMP, $6, $7, 'Pending')
            RETURNING {_HEADER_COLS}
            """,
            data.transfer_out_id, transfer_out["challan_no"], data.grn_number,
            data.receiving_warehouse, data.received_by,
            data.box_condition, data.condition_remarks,
        )

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

    async with conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO interunit_transfer_in_boxes
                (header_id, box_id, article, batch_number, lot_number,
                 transaction_no, net_weight, gross_weight,
                 scanned_at, is_matched, transfer_out_box_id, issue, line_index, scan_source)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP, $9, $10, $11, $12, $13)
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
            getattr(data, "scan_source", None) or "manual",
        )

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


async def create_transfer_in(conn, data) -> dict:
    # Fallback bulk receipt (no pending header) — also posts stock via
    # pick_from_pending + inserts scanned boxes. Less-used than the
    # pending→acknowledge→finalize path; deferred until its input schema lands.
    raise HTTPException(501, "Bulk transfer-in create is not yet wired (use the pending → acknowledge → finalize flow)")


async def get_pending_by_transfer_out(conn, transfer_out_id: int) -> dict:
    """Resume lookup: the Pending transfer-in header (+boxes) for a transfer-out."""
    row = await conn.fetchrow(
        f"""
        SELECT {_HEADER_COLS}
        FROM interunit_transfer_in_header
        WHERE transfer_out_id = $1 AND status = 'Pending'
        """,
        transfer_out_id)
    if not row:
        return {"exists": False, "header": None}
    header = await _header_with_boxes(conn, row["id"])
    return {"exists": True, "header": header}
