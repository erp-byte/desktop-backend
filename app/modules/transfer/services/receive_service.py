"""Transfer-IN receive lifecycle (pending header + box acknowledgement).

Ported from interunit_tools.py (create_pending_transfer_in, acknowledge_pending_box,
acknowledge_pending_boxes_batch, unacknowledge_pending_box, get_pending_by_transfer_out)
to asyncpg.

RECORD ONLY, the whole receive lifecycle. The stock tables ({cfpl,cdpl}_boxes_v2,
_bulk_entry_boxes, _cold_stocks) are kept by IMS and nothing here writes them:
  - create pending / acknowledge / unacknowledge touch only interunit_transfer_in_header
    and interunit_transfer_in_boxes;
  - finalize / close-with-shortage also clear the transfer's in-transit rows
    (pending_transfer_stock, stock_service.pick_from_pending) and flip the transfer-out
    to Received — nothing is posted to a destination stock table;
  - re-open / per-box edit re-create those in-transit rows (reversal_service.
    unpick_to_pending) — nothing is taken back out of a stock table.
The STBR reconciliation block (which remaps pending_transfer_stock on a box-id
mismatch) is not ported — see the STBR note below.

A transfer is received at its own To site: a receipt is begun only by a user who may
receive there (create_pending_transfer_in checks the transfer's stored to_site, never the
client's receiving_warehouse) and stores that site by its canonical name; each later step
of it (acknowledge, un-acknowledge, finalize, close with shortage, re-open, edit) is held
to the same lock when the router passes its user (_assert_receives_into). A receipt
records the transfer's boxes box by box — keyed by the box row, never by the box_id alone
(acknowledge_pending_box) — and close-with-shortage matches them the same way
(lookup_service._acknowledges), as R3 reads them.
"""
from __future__ import annotations

import json

from fastapi import HTTPException

from asyncpg.exceptions import UniqueViolationError

from app.core import sites
from app.core.helpers import insert_with_pk_retry, new_short_time_id
from app.modules.auth.middleware import AuthUser
from app.modules.transfer import permissions
from app.modules.transfer.services import create_service, lookup_service, reversal_service, stock_service
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


async def _assert_receives_into(conn, user: AuthUser | None, transfer_out_id) -> None:
    """With `user` (the router's caller): 403 unless they may receive into the stored To
    site of the transfer `transfer_out_id` (permissions.assert_can_receive_into), the
    lock its receipt was begun under (create_pending_transfer_in), so every later step of
    the receipt is held to it too. Admins and users with no warehouse lock pass without
    a read."""
    if user is None or not permissions.has_warehouse_lock(user):
        return
    to_site = await conn.fetchval("SELECT to_site FROM interunit_transfers_header WHERE id = $1", transfer_out_id)
    permissions.assert_can_receive_into(user, to_site)


async def _header_with_boxes(conn, header_id: int) -> dict:
    row = await conn.fetchrow(
        f"SELECT {_HEADER_COLS} FROM interunit_transfer_in_header WHERE id = $1", header_id)
    result = _map_transfer_in_header(dict(row))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def create_pending_transfer_in(conn, data, user: AuthUser) -> dict:
    """Create a Pending transfer-in header. Idempotent: returns the existing
    pending header if one already exists for the transfer-out.

    One transaction that locks the transfer-out's header row first (FOR UPDATE): the
    scan screen changes a request's transfer only while it has no receipt and checks
    that under the same lock (scan_service), so no box is added to, or taken off, a
    transfer once its receipt has begun.

    A transfer is received at its own To site: under that lock, the user must be able to
    receive into the transfer's stored to_site (permissions.assert_can_receive_into, 403;
    sites compared as sites, so a user of A185 receives a legacy 'Warehouse A185'
    transfer), whatever receiving_warehouse the client sends, and the receipt stores the
    To site by its canonical name (sites.canonical_site), not the client's value."""
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
            sites.canonical_site(transfer_out["to_site"]), data.received_by,
            data.box_condition, data.condition_remarks, new_short_time_id(),
        )
    async with conn.transaction():
        transfer_out = await conn.fetchrow(
            "SELECT id, challan_no, to_site FROM interunit_transfers_header WHERE id = $1 FOR UPDATE",
            data.transfer_out_id)
        if not transfer_out:
            raise HTTPException(404, "Transfer OUT not found")
        permissions.assert_can_receive_into(user, transfer_out["to_site"])

        existing_in = await conn.fetchrow(
            "SELECT id, status FROM interunit_transfer_in_header WHERE transfer_out_id = $1",
            data.transfer_out_id)
        if existing_in and existing_in["status"] != "Pending":
            raise HTTPException(400, "Transfer OUT already has a completed Transfer IN (GRN) record")
        if not existing_in:
            existing_grn = await conn.fetchrow(
                "SELECT id FROM interunit_transfer_in_header WHERE grn_number = $1", data.grn_number)
            if existing_grn:
                raise HTTPException(400, f"GRN number {data.grn_number} already exists")
            header = await insert_with_pk_retry(conn, _ins_in_header)

    if existing_in:
        return await _header_with_boxes(conn, existing_in["id"])
    result = _map_transfer_in_header(dict(header))
    result["boxes"] = []
    result["total_boxes_scanned"] = 0
    return result


# The columns an acknowledgement returns (the receipt box row as the screens read it).
_IN_BOX_COLS = ("id, header_id, box_id, article, batch_number, lot_number, transaction_no, net_weight, "
                "gross_weight, scanned_at, is_matched, transfer_out_box_id, issue, line_index")


def _receipt_row_of(rows: list, box, box_id, key, others: set):
    """The receipt box row of `rows` (this receipt's) that records the box being
    acknowledged: the one naming its box row (`box`, the transfer's; transfer_out_box_id),
    else one with its box_id and transaction_no, trimmed, else one with its box_id that
    records no other box of the transfer (`others`: their keys) — this box recorded
    before under a blank or another transaction (an older client, IMS). For an entry that
    is not a real box (key None: a quantity-only line) the one with its box_id as sent.
    None when the receipt has none yet."""
    if box is not None:
        hit = next((r for r in rows if r.get("transfer_out_box_id") == box["id"]), None)
        if hit is not None:
            return hit
    if key is None:
        return next((r for r in rows if r["box_id"] == box_id), None)
    hit = next((r for r in rows if lookup_service._box_key(r["box_id"], r["transaction_no"]) == key), None)
    if hit is not None:
        return hit
    return next((r for r in rows if (r["box_id"] or "").strip() == key[0]
                 and r.get("transfer_out_box_id") in (None, box["id"] if box else None)
                 and lookup_service._box_key(r["box_id"], r["transaction_no"]) not in others), None)


async def acknowledge_pending_box(conn, header_id: int, data, user: AuthUser | None = None) -> dict:
    """Record a single box/article on a pending transfer-in (record-only): its receipt
    box row is written, or rewritten when the receipt already records that box. With
    `user`, they must be able to receive into the transfer's To site (403,
    _assert_receives_into).

    One transaction with the receipt's header row locked (FOR UPDATE), so acknowledgements
    of one receipt run one after another. A real box (box_id + transaction_no, not
    'DIRECT') must be a box of the transfer (409 otherwise), found by its box_id and
    transaction_no (trimmed) or by transfer_out_box_id, and its row records that box row
    as transfer_out_box_id. The receipt row is keyed by that box, never by the box_id
    alone (_receipt_row_of): two boxes of one transfer that share a box_id under
    different transactions are two rows, and one never overwrites the other. Where the
    database still holds one row per box_id per receipt (the unique index IMS made), the
    second such box is refused (409) instead of overwriting the first.

    NOTE: STBR (Scan-Time Box-ID Reconciliation) is intentionally NOT run here — it
    remaps pending_transfer_stock (the in-transit rows) on a box-id swap and is not
    ported. A box that is not on the transfer is refused instead.
    """
    issue_json = json.dumps(data.issue) if data.issue else None
    key = lookup_service._box_key(data.box_id, data.transaction_no)
    values = (data.box_id, data.article, data.batch_number, data.lot_number, data.transaction_no,
              data.net_weight, data.gross_weight, data.is_matched)
    tail = (issue_json, data.line_index, getattr(data, "scan_source", None) or "manual")

    async with conn.transaction():
        header = await conn.fetchrow(
            "SELECT id, status, transfer_out_id, transfer_out_no FROM interunit_transfer_in_header "
            "WHERE id = $1 FOR UPDATE",
            header_id)
        if not header:
            raise HTTPException(404, "Transfer IN header not found")
        await _assert_receives_into(conn, user, header["transfer_out_id"])
        if header["status"] != "Pending":
            raise HTTPException(400, "Transfer IN is not in Pending status")

        out_boxes = await conn.fetch(
            "SELECT id, box_id, transaction_no FROM interunit_transfer_boxes WHERE header_id = $1",
            header["transfer_out_id"])
        box = None
        if key is not None:
            mine = [b for b in out_boxes if lookup_service._box_key(b["box_id"], b["transaction_no"]) == key]
            box = next((b for b in mine if b["id"] == data.transfer_out_box_id), mine[0] if mine else None)
            if box is None:
                raise HTTPException(
                    409, f"Box {key[0]} with transaction {key[1]} is not on transfer "
                         f"{header['transfer_out_no'] or header['transfer_out_id']}.")
        elif data.transfer_out_box_id is not None:
            box = next((b for b in out_boxes if b["id"] == data.transfer_out_box_id), None)
        out_box_id = box["id"] if box is not None else data.transfer_out_box_id

        rows = await conn.fetch("SELECT * FROM interunit_transfer_in_boxes WHERE header_id = $1", header_id)
        others = {k for k in (lookup_service._box_key(b["box_id"], b["transaction_no"]) for b in out_boxes)
                  if k and k != key}
        existing = _receipt_row_of(rows, box, data.box_id, key, others)
        if existing is not None:
            row = await conn.fetchrow(
                f"""
                UPDATE interunit_transfer_in_boxes
                SET box_id = $2, article = $3, batch_number = $4, lot_number = $5,
                    transaction_no = $6, net_weight = $7, gross_weight = $8, is_matched = $9,
                    transfer_out_box_id = $10, issue = $11, line_index = $12,
                    scanned_at = CURRENT_TIMESTAMP, scan_source = $13
                WHERE id = $1
                RETURNING {_IN_BOX_COLS}
                """,
                existing["id"], *values, out_box_id, *tail)
        else:
            async def _ins_in_box():
                return await conn.fetchrow(
                    f"""
                    INSERT INTO interunit_transfer_in_boxes
                        (header_id, box_id, article, batch_number, lot_number,
                         transaction_no, net_weight, gross_weight,
                         scanned_at, is_matched, transfer_out_box_id, issue, line_index, scan_source, id)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_TIMESTAMP, $9, $10, $11, $12, $13, $14)
                    RETURNING {_IN_BOX_COLS}
                    """,
                    header_id, *values, out_box_id, *tail, new_short_time_id(),
                )
            try:
                row = await insert_with_pk_retry(conn, _ins_in_box)
            except UniqueViolationError as exc:
                if "pkey" in (getattr(exc, "constraint_name", "") or ""):
                    raise
                other = next((r for r in rows if (r["box_id"] or "").strip() == (data.box_id or "").strip()), None)
                raise HTTPException(
                    409, f"This receipt already records box {(data.box_id or '').strip()}"
                         + (f" (transaction {(other['transaction_no'] or '').strip()})" if other else "")
                         + "; a second box with the same box id can't be recorded on it.")

    result = _map_transfer_in_box(dict(row))
    # STBR deferred (P8b-2) — report a no-op so the frontend contract holds.
    result["reconciliation"] = {
        "status": "noop", "original_box_id": None,
        "propagated_count": 0, "siblings": [], "reconciliation_id": None,
    }
    return result


async def acknowledge_pending_boxes_batch(conn, header_id: int, boxes: list, user: AuthUser | None = None) -> dict:
    """Batch acknowledge — per-box, surfacing per-row failures rather than
    failing the whole batch (mirrors the reference). A user who may not receive into
    the transfer's To site is refused the whole batch (403, before any box)."""
    results, conflicts = [], []
    for box in boxes:
        try:
            results.append(await acknowledge_pending_box(conn, header_id, box, user=user))
        except HTTPException as e:
            conflicts.append({
                "box_id": getattr(box, "box_id", None),
                "transaction_no": getattr(box, "transaction_no", None),
                "status_code": e.status_code,
                "detail": e.detail,
            })
    return {"success": len(conflicts) == 0, "count": len(results),
            "boxes": results, "conflicts": conflicts}


async def unacknowledge_pending_box(conn, header_id: int, box_id: str,
                                    transaction_no: str | None = None, user: AuthUser | None = None) -> dict:
    """Take a box off a pending transfer-in: its receipt box row(s) with this box_id,
    or, with `transaction_no`, only the one under that transaction (trimmed), so of two
    boxes sharing a box_id only the one named goes. With `user`, the receive lock
    (_assert_receives_into, 403)."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id FROM interunit_transfer_in_header WHERE id = $1", header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    await _assert_receives_into(conn, user, header["transfer_out_id"])
    if header["status"] != "Pending":
        raise HTTPException(400, "Transfer IN is not in Pending status")

    if transaction_no is None:
        deleted = await conn.fetchrow(
            "DELETE FROM interunit_transfer_in_boxes WHERE header_id = $1 AND box_id = $2 RETURNING id",
            header_id, box_id)
    else:
        ids = [r["id"] for r in await conn.fetch(
            "SELECT id, transaction_no FROM interunit_transfer_in_boxes WHERE header_id = $1 AND box_id = $2",
            header_id, box_id) if (r["transaction_no"] or "").strip() == transaction_no.strip()]
        if ids:
            await conn.execute("DELETE FROM interunit_transfer_in_boxes WHERE id = ANY($1::bigint[])", ids)
        deleted = bool(ids)
    if not deleted:
        raise HTTPException(404, f"Box {box_id} not found in this transfer-in")
    return {"success": True, "deleted_box_id": box_id}


async def finalize_transfer_in(conn, header_id: int, data, user: AuthUser | None = None) -> dict:
    """Finalize a Pending transfer-in → Received: clears the transfer's in-transit rows
    (pick_from_pending deletes its pending_transfer_stock rows) and flips the
    transfer-out to Received. Record only — no stock table is written (IMS keeps
    stock); re-open undoes it. All effects run in one transaction. With `user`, the
    receive lock (_assert_receives_into, 403).

    `cold_storage_items` (the reference's legacy cold-stock insert, for transfers
    dispatched before pending_transfer_stock existed) is accepted and ignored."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id, transfer_out_no FROM interunit_transfer_in_header WHERE id = $1",
        header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    await _assert_receives_into(conn, user, header["transfer_out_id"])
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

        await stock_service.pick_from_pending(conn, header["transfer_out_id"])

        await conn.execute(
            "UPDATE interunit_transfers_header SET status = 'Received' WHERE id = $1",
            header["transfer_out_id"])

    result = _map_transfer_in_header(dict(updated))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def reopen_transfer_in(conn, header_id: int, reopened_by: str, user: AuthUser | None = None) -> dict:
    """Re-open a Received transfer-in → Pending. Puts its boxes back in transit
    (unpick_to_pending re-creates their pending rows; record only, no stock table is
    touched) and flips both headers — the transfer-out back to in transit, Dispatch or
    Partial (create_service.in_transit_status) — KEEPING the GRN and its acknowledged
    boxes so the operator can correct lots / raise issues. One txn. With `user`, the
    receive lock (_assert_receives_into, 403)."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id FROM interunit_transfer_in_header WHERE id = $1", header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    await _assert_receives_into(conn, user, header["transfer_out_id"])
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
            "UPDATE interunit_transfers_header SET status = $1 WHERE id = $2",
            await create_service.in_transit_status(conn, header["transfer_out_id"]), header["transfer_out_id"])

    result = _map_transfer_in_header(dict(updated))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def close_with_shortage(conn, header_id: int, shortage_reason: str | None, closed_by: str,
                              user: AuthUser | None = None) -> dict:
    """Close a Pending transfer-in as Received WITH A SHORTAGE: clear the acknowledged
    boxes' in-transit rows, then write off the rest (dispatched but never arrived) by
    deleting their pending rows and noting the count on the GRN. Record only — no stock
    table is written either way. One txn. With `user`, the receive lock
    (_assert_receives_into, 403)."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id, condition_remarks FROM interunit_transfer_in_header WHERE id = $1",
        header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    await _assert_receives_into(conn, user, header["transfer_out_id"])
    if header["status"] != "Pending":
        raise HTTPException(400, "Transfer IN is not in Pending status")

    ack_rows = [dict(r) for r in await conn.fetch(
        "SELECT * FROM interunit_transfer_in_boxes WHERE header_id = $1", header_id)]
    if not any((r.get("box_id") or "").strip() for r in ack_rows):
        raise HTTPException(400, "Acknowledge at least one box before closing with shortage.")

    async with conn.transaction():
        # The in-transit rows of the boxes the receipt acknowledges, matched as R3 matches
        # them (lookup_service._acknowledges: the box row it names, else box_id and
        # transaction_no), so of two boxes sharing a box_id only the acknowledged one is
        # received.
        tout = header["transfer_out_id"]
        out_rows = {}
        for b in await conn.fetch(
                "SELECT id, box_id, transaction_no FROM interunit_transfer_boxes WHERE header_id = $1", tout):
            key = lookup_service._box_key(b["box_id"], b["transaction_no"])
            if key:
                out_rows.setdefault(key, b["id"])
        in_transit = await conn.fetch(
            "SELECT id, box_id, transaction_no FROM pending_transfer_stock "
            "WHERE transfer_out_id = $1 AND status = 'In Transit'", tout)
        arrived = [p["id"] for p in in_transit if any(
            lookup_service._acknowledges(r, out_rows.get(lookup_service._box_key(p["box_id"], p["transaction_no"])),
                                         p["box_id"], p["transaction_no"]) for r in ack_rows)]
        await stock_service.pick_from_pending(conn, tout, row_ids=arrived)
        # Write off the boxes that never arrived (the remaining In-Transit rows).
        written = stock_service._rows_affected(await conn.execute(
            "DELETE FROM pending_transfer_stock WHERE transfer_out_id = $1 AND status = 'In Transit'",
            header["transfer_out_id"]))
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


def _receiving_warehouse(value, stored, to_site):
    """The receiving_warehouse an edit of a receipt writes: None (the stored one stays)
    when the client sends none, or one that names the stored site; the transfer's To
    site by canonical name when it names that site (sites.same_site); else 409 — a
    receipt is at its transfer's To site."""
    if not sites.site_key(value) or sites.same_site(value, stored):
        return None
    if sites.same_site(value, to_site):
        return sites.canonical_site(to_site)
    raise HTTPException(
        409, f"This receipt is at {sites.canonical_site(to_site) or '—'}, its transfer's To site; "
             f"it can't be moved to {sites.canonical_site(value)}.")


async def edit_transfer_in(conn, header_id: int, data, edited_by: str, user: AuthUser | None = None) -> dict:
    """Edit a Received transfer-in. HEADER-only edits (grn / receiving warehouse /
    condition / remarks) update in place. PER-BOX edits (lot / weight / article) go
    through re-open → correct the boxes + pending rows → re-finalize, as a re-open and
    a finalize would. Record only — no stock table is written. One transaction.

    The receiving warehouse stays the transfer's To site (_receiving_warehouse: 409 for
    another site). With `user`, the user must be able to receive into that To site
    (permissions.assert_can_receive_into, 403), as when the receipt was begun."""
    header = await conn.fetchrow(
        "SELECT id, status, transfer_out_id, receiving_warehouse FROM interunit_transfer_in_header WHERE id = $1",
        header_id)
    if not header:
        raise HTTPException(404, "Transfer IN header not found")
    if header["status"] != "Received":
        raise HTTPException(400, "Only a Received transfer-in can be edited")
    tout_id = header["transfer_out_id"]
    box_edits = data.boxes or []
    to_site = await conn.fetchval("SELECT to_site FROM interunit_transfers_header WHERE id = $1", tout_id)
    if user is not None:
        permissions.assert_can_receive_into(user, to_site)
    receiving_warehouse = _receiving_warehouse(data.receiving_warehouse, header["receiving_warehouse"], to_site)

    async with conn.transaction():
        if box_edits:
            # Back in transit, correct the boxes + pending rows, receive again (record only).
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
                header_id, data.grn_number, receiving_warehouse, data.box_condition, data.condition_remarks)
            await conn.execute(
                "UPDATE interunit_transfers_header SET status = 'Received' WHERE id = $1", tout_id)
        else:
            # Header-only edit — stays Received.
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
                header_id, data.grn_number, receiving_warehouse, data.box_condition, data.condition_remarks)

    result = _map_transfer_in_header(dict(updated))
    result["boxes"] = await _fetch_transfer_in_boxes_q(conn, header_id)
    result["total_boxes_scanned"] = len(result["boxes"])
    return result


async def create_transfer_in(conn, data, user: AuthUser) -> dict:
    # Fallback bulk receipt (no pending header) — would clear the in-transit rows via
    # pick_from_pending + insert the scanned boxes (record only). Less-used than the
    # pending→acknowledge→finalize path; deferred until its input schema lands. When it
    # is wired it receives as create_pending_transfer_in does: with the transfer's header
    # row locked, `user` checked against the transfer's stored to_site (never the
    # client's receiving_warehouse), which is stored by its canonical name.
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
