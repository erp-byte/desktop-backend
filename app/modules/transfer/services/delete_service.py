"""Delete paths for requests / transfers / transfer-ins.

Ported from interunit_tools.py. Authorization is enforced at the router via the
per-action asserts in permissions.py; these functions own the data effects.

delete_request deletes only a Pending or Rejected request. delete_transfer drops the
transfer's in-transit records (restore_to_source) with it and puts its request back to
Approved (Ready to Send), and delete_transfer_in puts the transfer's boxes back in
transit (unpick_to_pending) — each atomic. Record only: the stock tables are IMS's and
nothing here writes them (a dispatch never took the box out, a receipt never put it in).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from app.modules.transfer.services import create_service, reversal_service


# A request may be deleted only before anyone has acted on it, or once it is rejected.
_DELETABLE = ("Pending", "Rejected")


async def delete_request(conn, request_id: int) -> dict:
    """Delete a request and its lines, only while it is Pending (the store has not
    accepted it) or Rejected: 409 for an Accepted, Approved or Transferred one, and for
    one that still has a transfer (legacy data). The row is locked first, so a delete
    never runs alongside a decision or a Scan & send save of the same request."""
    async with conn.transaction():
        req = await conn.fetchrow(
            "SELECT id, request_no, status FROM interunit_transfer_requests WHERE id = $1 FOR UPDATE",
            request_id)
        if not req:
            raise HTTPException(404, "Request not found")
        no, status = req["request_no"] or str(request_id), req["status"] or "Pending"
        if status not in _DELETABLE:
            raise HTTPException(
                409, f"Request {no} is {status}; only a Pending or Rejected request can be deleted.")
        if await conn.fetchval(
                "SELECT 1 FROM interunit_transfers_header WHERE request_id = $1 LIMIT 1", request_id):
            raise HTTPException(409, f"Request {no} has a transfer; delete the transfer first.")
        await conn.execute(
            "DELETE FROM interunit_transfer_request_lines WHERE request_id = $1", request_id)
        await conn.execute(
            "DELETE FROM interunit_transfer_requests WHERE id = $1", request_id)

    return {"success": True, "message": "Request deleted successfully"}


async def _delete_transfer_rows(conn, transfer_id: int) -> int:
    """delete_transfer's work, inside the caller's transaction (delete_transfer; the scan
    screen removing a transfer's last box, scan_service): drop each Transfer-In's boxes,
    drop the pending rows (restore_to_source), then delete the Transfer-Ins and the
    transfer's boxes/lines/header. Returns how many Transfer-In records went with it.

    A receipt is not undone first (unpick_to_pending): its boxes would only be put back
    in transit on this transfer to be dropped with it, and its take-over would delete an
    in-transit row an older transfer of one of its boxes is left holding."""
    ti_headers = await conn.fetch(
        "SELECT id FROM interunit_transfer_in_header WHERE transfer_out_id = $1", transfer_id)
    # 1. Drop every Transfer-In's boxes.
    for ti in ti_headers:
        await conn.execute(
            "DELETE FROM interunit_transfer_in_boxes WHERE header_id = $1", ti["id"])
    # 2. Drop every pending row (nothing goes back to stock: none was taken).
    await reversal_service.restore_to_source(conn, transfer_id)
    # 3. Delete in FK order.
    await conn.execute(
        "DELETE FROM interunit_transfer_in_header WHERE transfer_out_id = $1", transfer_id)
    await conn.execute(
        "DELETE FROM interunit_transfer_boxes WHERE header_id = $1", transfer_id)
    await conn.execute(
        "DELETE FROM interunit_transfers_lines WHERE header_id = $1", transfer_id)
    await conn.execute(
        "DELETE FROM interunit_transfers_header WHERE id = $1", transfer_id)
    return len(ti_headers)


async def _free_request(conn, request_id: int) -> str | None:
    """After a request's transfer is deleted: put the request back from 'Transferred' to
    'Approved' (Ready to Send) when no other transfer of it is left, so it can be scanned
    and sent again; the store's and the inventory manager's decisions stay as they were.
    Returns the request's number when it was put back, else None. The caller holds the
    request row's lock."""
    if await conn.fetchval(
            "SELECT 1 FROM interunit_transfers_header WHERE request_id = $1 LIMIT 1", request_id):
        return None
    row = await conn.fetchrow(
        "UPDATE interunit_transfer_requests SET status = 'Approved', updated_at = $1 "
        "WHERE id = $2 AND status = 'Transferred' RETURNING request_no",
        datetime.now(), request_id)
    return (row["request_no"] or str(request_id)) if row else None


async def delete_transfer(conn, transfer_id: int) -> dict:
    """Delete a transfer-out and everything downstream (_delete_transfer_rows). All in one
    transaction, record only — no stock is put back because none was taken. Its boxes
    are then free to go on another transfer, and its request is back in Ready to Send
    (_free_request). A transfer from a request has its request row locked first
    (create_service._LOCK_REQUEST_OF), as the scan screen's saves of it do, so a delete
    never runs in the middle of one."""
    read = "SELECT id, challan_no, status, request_id FROM interunit_transfers_header WHERE id = $1"
    existing = await conn.fetchrow(read, transfer_id)
    if not existing:
        raise HTTPException(404, "Transfer not found")

    async with conn.transaction():
        if existing["request_id"] is not None:
            # Read again under the lock: the scan screen may have just cancelled it.
            await conn.fetchrow(create_service._LOCK_REQUEST_OF, transfer_id)
            if not await conn.fetchrow(read, transfer_id):
                raise HTTPException(404, "Transfer not found")
        ti_count = await _delete_transfer_rows(conn, transfer_id)
        freed = (await _free_request(conn, existing["request_id"])
                 if existing["request_id"] is not None else None)

    msg = "Transfer deleted successfully"
    if ti_count:
        msg += f" (along with {ti_count} transfer-in record{'s' if ti_count > 1 else ''})"
    if freed:
        msg += f". Request {freed} is back in Ready to Send"
    return {"success": True, "message": msg,
            "transfer_id": existing["id"], "challan_no": existing["challan_no"]}


async def delete_transfer_in(conn, transfer_in_id: int, user_email: str) -> dict:
    """Delete a Transfer-In: reverse the receive (unpick_to_pending — its boxes back
    in transit; record only, no stock table is touched), drop the GRN, and revert the
    transfer-out to in transit: 'Dispatch', or 'Partial' for one from a request that has
    fewer boxes than the request asks for (create_service.in_transit_status).
    Authorization is enforced at the router (admins and inventory managers)."""
    header = await conn.fetchrow(
        "SELECT id, grn_number, transfer_out_id, transfer_out_no FROM interunit_transfer_in_header WHERE id = $1",
        transfer_in_id)
    if not header:
        raise HTTPException(404, "Transfer IN not found")
    to_id = header["transfer_out_id"]

    async with conn.transaction():
        if to_id:
            await reversal_service.unpick_to_pending(conn, transfer_in_id, to_id)
        await conn.execute(
            "DELETE FROM interunit_transfer_in_boxes WHERE header_id = $1", transfer_in_id)
        await conn.execute(
            "DELETE FROM interunit_transfer_in_header WHERE id = $1", transfer_in_id)
        if to_id:
            await conn.execute(
                "UPDATE interunit_transfers_header SET status = $1 WHERE id = $2",
                await create_service.in_transit_status(conn, to_id), to_id)

    return {"success": True, "message": "Transfer-in deleted successfully"}
