"""P8b-1 review: exercise the pending-receive lifecycle against the LIVE DB
inside a transaction that is ROLLED BACK — so create_pending/acknowledge/unack
run on real rows but NOTHING persists. Then assert zero leakage."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv
from fastapi import HTTPException

from app.modules.transfer import schemas
from app.modules.transfer.services import receive_service as r

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]
ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")
    ok += 1 if cond else 0
    fail += 0 if cond else 1


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        tid = await conn.fetchval(
            """
            SELECT h.id FROM interunit_transfers_header h
            WHERE NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header t WHERE t.transfer_out_id = h.id)
            ORDER BY h.id DESC LIMIT 1
            """)
        if tid is None:
            print("No transfer-out without a transfer-in available; skipping create path.")
            return
        grn = f"GRN-RBK-{tid}"
        print(f"Using transfer_out_id={tid}, grn={grn} (all rolled back)")

        tr = conn.transaction()
        await tr.start()
        try:
            data = schemas.PendingTransferInCreate(
                transfer_out_id=tid, grn_number=grn,
                receiving_warehouse="TEST WH", received_by="HARNESS")
            hdr = await r.create_pending_transfer_in(conn, data)
            chk("create_pending -> Pending", hdr["status"] == "Pending", f"id={hdr['id']}")
            hid = hdr["id"]

            hdr2 = await r.create_pending_transfer_in(conn, data)
            chk("create_pending idempotent", hdr2["id"] == hid)

            box = schemas.PendingBoxAcknowledge(
                box_id="RBK-BOX-1", article="TEST ITEM", lot_number="L1",
                transaction_no="DIRECT", net_weight=10.5, gross_weight=11.0, is_matched=True)
            ack = await r.acknowledge_pending_box(conn, hid, box)
            chk("acknowledge box (no-op STBR)",
                ack["box_id"] == "RBK-BOX-1" and ack["reconciliation"]["status"] == "noop")

            # Re-ack same box -> UPSERT (still 1 box)
            await r.acknowledge_pending_box(conn, hid, box)

            b2 = schemas.PendingBoxAcknowledge(
                box_id="RBK-BOX-2", article="TEST ITEM", transaction_no="DIRECT",
                net_weight=9.0, is_matched=True)
            batch = await r.acknowledge_pending_boxes_batch(conn, hid, [b2])
            chk("batch ack ok", batch["success"] and batch["count"] == 1)

            gp = await r.get_pending_by_transfer_out(conn, tid)
            chk("get_pending exists, 2 boxes", gp["exists"] and gp["header"]["total_boxes_scanned"] == 2,
                f"boxes={gp['header']['total_boxes_scanned']}")

            un = await r.unacknowledge_pending_box(conn, hid, "RBK-BOX-1")
            chk("unacknowledge", un["success"])
            gp2 = await r.get_pending_by_transfer_out(conn, tid)
            chk("after unack -> 1 box", gp2["header"]["total_boxes_scanned"] == 1)

            un_missing_ok = False
            try:
                await r.unacknowledge_pending_box(conn, hid, "DOES-NOT-EXIST")
            except HTTPException as e:
                un_missing_ok = e.status_code == 404
            chk("unack missing box -> 404", un_missing_ok)

            for name, coro in [
                ("finalize -> 501", r.finalize_transfer_in(conn, hid, schemas.FinalizeTransferIn())),
                ("create_transfer_in -> 501", r.create_transfer_in(conn, {})),
            ]:
                got = None
                try:
                    await coro
                except HTTPException as e:
                    got = e.status_code
                chk(name, got == 501)
        finally:
            await tr.rollback()
            print("  -- transaction ROLLED BACK --")

        leaked_hdr = await conn.fetchval(
            "SELECT COUNT(*) FROM interunit_transfer_in_header WHERE grn_number = $1", grn)
        chk("no header leaked after rollback", leaked_hdr == 0)
        leaked_box = await conn.fetchval(
            "SELECT COUNT(*) FROM interunit_transfer_in_boxes WHERE box_id IN ('RBK-BOX-1','RBK-BOX-2')")
        chk("no boxes leaked after rollback", leaked_box == 0)
    finally:
        await conn.close()
    print(f"\nP8b-1 REVIEW: {ok} passed, {fail} failed")


asyncio.run(main())
