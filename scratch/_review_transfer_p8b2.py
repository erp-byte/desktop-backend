"""P8b-2 review (rollback-verified, nothing persists):
  Test A — finalize on a REAL warehouse-dest in-transit transfer: pick_from_pending
           deletes pending rows + flips statuses; rollback restores them.
  Test B — finalize on a SYNTHETIC cold-dest pending row (inserted in-txn): exercises
           the cold_stocks INSERT path (type coercion, JSON decode, 19-col insert);
           rollback removes the cold_stocks row.
Both run inside transactions that are ROLLED BACK."""
import asyncio
import json
import os
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv

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
        # ── Test A: real warehouse-dest transfer ──
        print("Test A — finalize on a real in-transit transfer (warehouse dest):")
        a = await conn.fetchrow(
            """
            SELECT pts.transfer_out_id tid, COUNT(*) n
            FROM pending_transfer_stock pts
            WHERE pts.status='In Transit'
              AND NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header t WHERE t.transfer_out_id=pts.transfer_out_id)
            GROUP BY pts.transfer_out_id ORDER BY n ASC LIMIT 1
            """)
        if a:
            tid, n = a["tid"], a["n"]
            orig_status = await conn.fetchval("SELECT status FROM interunit_transfers_header WHERE id=$1", tid)
            tr = conn.transaction(); await tr.start()
            try:
                hdr = await r.create_pending_transfer_in(conn, schemas.PendingTransferInCreate(
                    transfer_out_id=tid, grn_number=f"GRN-A-{tid}", receiving_warehouse="WH", received_by="H"))
                await r.acknowledge_pending_box(conn, hdr["id"], schemas.PendingBoxAcknowledge(
                    box_id="A-BOX-1", transaction_no="DIRECT", net_weight=1.0, is_matched=True))
                fin = await r.finalize_transfer_in(conn, hdr["id"], schemas.FinalizeTransferIn(box_condition="Good"))
                chk("A: header Received", fin["status"] == "Received")
                left = await conn.fetchval("SELECT COUNT(*) FROM pending_transfer_stock WHERE transfer_out_id=$1 AND status='In Transit'", tid)
                chk("A: pending moved (0 left)", left == 0, f"was {n}")
                chk("A: transfer-out Received",
                    (await conn.fetchval("SELECT status FROM interunit_transfers_header WHERE id=$1", tid)) == "Received")
            finally:
                await tr.rollback()
            chk("A: pending restored after rollback",
                (await conn.fetchval("SELECT COUNT(*) FROM pending_transfer_stock WHERE transfer_out_id=$1 AND status='In Transit'", tid)) == n)
            chk("A: transfer-out status restored",
                (await conn.fetchval("SELECT status FROM interunit_transfers_header WHERE id=$1", tid)) == orig_status)
        else:
            print("  (no eligible real transfer — skipped)")

        # ── Test B: synthetic cold-dest pending row → exercises cold_stocks INSERT ──
        print("Test B — finalize posting a synthetic cold_stocks row:")
        # Only requirement: no existing transfer-in (so create_pending works). The
        # synthetic cold row is what we assert on; any other pending rows for this
        # transfer-out are picked too but all rolled back.
        tidB = await conn.fetchval(
            """
            SELECT h.id FROM interunit_transfers_header h
            WHERE NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header t WHERE t.transfer_out_id=h.id)
            ORDER BY h.id DESC LIMIT 1
            """)
        if tidB:
            cold_data = json.dumps({
                "unit": "Rishi", "storage_location": "Rishi", "inward_dt": "2026-02-17",
                "vakkal": "V1", "item_mark": "M1", "group_name": "G", "item_subgroup": "SG",
                "exporter": "E", "last_purchase_rate": 10.5, "value": 105.0,
                "total_inventory_kgs": 10.0, "spl_remarks": "synthetic",
            })
            tr = conn.transaction(); await tr.start()
            try:
                await conn.execute(
                    """
                    INSERT INTO pending_transfer_stock
                        (transfer_type, transfer_out_id, transfer_out_challan_no, box_id, transaction_no,
                         from_site, to_site, from_storage_type, to_storage_type, source_table, destination_table,
                         item_description, lot_no, weight_kg, no_of_cartons, cold_storage_data,
                         status, dispatched_at, dispatched_by)
                    VALUES ('cold', $1, $2, 'SYNTH-COLD-BOX-1', 'SYNTH-TXN-1', 'Cold Storage', 'Rishi',
                            'cold','cold','cfpl_cold_stocks','cfpl_cold_stocks','SYNTH ITEM','LOT-S',$3,1,$4::jsonb,
                            'In Transit', NOW(), 'HARNESS')
                    """,
                    tidB, f"CH-{tidB}", Decimal("10.0"), cold_data)
                hdr = await r.create_pending_transfer_in(conn, schemas.PendingTransferInCreate(
                    transfer_out_id=tidB, grn_number=f"GRN-B-{tidB}", receiving_warehouse="WH", received_by="H"))
                await r.acknowledge_pending_box(conn, hdr["id"], schemas.PendingBoxAcknowledge(
                    box_id="B-BOX-1", transaction_no="DIRECT", net_weight=1.0, is_matched=True))
                await r.finalize_transfer_in(conn, hdr["id"], schemas.FinalizeTransferIn(box_condition="Good"))
                inserted = await conn.fetchval(
                    "SELECT COUNT(*) FROM cfpl_cold_stocks WHERE box_id='SYNTH-COLD-BOX-1' AND transaction_no='SYNTH-TXN-1'")
                chk("B: cold_stocks row inserted by pick_from_pending", inserted == 1, f"count={inserted}")
                synth_left = await conn.fetchval(
                    "SELECT COUNT(*) FROM pending_transfer_stock WHERE box_id='SYNTH-COLD-BOX-1' AND transaction_no='SYNTH-TXN-1'")
                chk("B: synthetic pending row consumed", synth_left == 0)
            finally:
                await tr.rollback()
            gone = await conn.fetchval(
                "SELECT COUNT(*) FROM cfpl_cold_stocks WHERE box_id='SYNTH-COLD-BOX-1' AND transaction_no='SYNTH-TXN-1'")
            chk("B: cold_stocks row gone after rollback", gone == 0, f"count={gone}")
        else:
            print("  (no eligible transfer for synthetic test — skipped)")
    finally:
        await conn.close()
    print(f"\nP8b-2 REVIEW: {ok} passed, {fail} failed")


asyncio.run(main())
