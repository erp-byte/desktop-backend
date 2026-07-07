"""Delete paths for requests / transfers / transfer-ins.

Ported from interunit_tools.py. Authorization is enforced at the router via the
per-action asserts in permissions.py; these functions own the data effects.

delete_transfer / delete_transfer_in unwind in-transit inventory
(restore_to_source / unpick_to_pending) before deleting — destructive, atomic.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.modules.transfer.services import reversal_service


async def delete_request(conn, request_id: int) -> dict:
    existing = await conn.fetchrow(
        "SELECT id FROM interunit_transfer_requests WHERE id = $1", request_id)
    if not existing:
        raise HTTPException(404, "Request not found")

    async with conn.transaction():
        await conn.execute(
            "DELETE FROM interunit_transfer_request_lines WHERE request_id = $1", request_id)
        await conn.execute(
            "DELETE FROM interunit_transfer_requests WHERE id = $1", request_id)

    return {"success": True, "message": "Request deleted successfully"}


async def delete_transfer(conn, transfer_id: int) -> dict:
    """Delete a transfer-out and everything downstream, restoring stock first:
    reverse each Transfer-In (unpick_to_pending), restore pending rows to source,
    then delete boxes/lines/header. All in one transaction."""
    existing = await conn.fetchrow(
        "SELECT id, challan_no, status FROM interunit_transfers_header WHERE id = $1", transfer_id)
    if not existing:
        raise HTTPException(404, "Transfer not found")

    async with conn.transaction():
        ti_headers = await conn.fetch(
            "SELECT id FROM interunit_transfer_in_header WHERE transfer_out_id = $1", transfer_id)
        # 1. Reverse every Transfer-In (destination → pending), then drop its boxes.
        for ti in ti_headers:
            await reversal_service.unpick_to_pending(conn, ti["id"], transfer_id)
            await conn.execute(
                "DELETE FROM interunit_transfer_in_boxes WHERE header_id = $1", ti["id"])
        # 2. Restore every pending row back to source.
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

    ti_count = len(ti_headers)
    msg = "Transfer deleted successfully"
    if ti_count:
        msg += f" (along with {ti_count} transfer-in record{'s' if ti_count > 1 else ''})"
    return {"success": True, "message": msg,
            "transfer_id": existing["id"], "challan_no": existing["challan_no"]}


async def delete_transfer_in(conn, transfer_in_id: int, user_email: str) -> dict:
    """Delete a Transfer-In: reverse the receive (unpick_to_pending — destination
    back to pending), drop the GRN, and revert the transfer-out to 'Dispatch'.
    Authorization is enforced at the router (assert_can_delete_transfer_in)."""
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
                "UPDATE interunit_transfers_header SET status = 'Dispatch' WHERE id = $1", to_id)

    return {"success": True, "message": "Transfer-in deleted successfully"}
