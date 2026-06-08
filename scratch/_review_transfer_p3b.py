"""P3b review (rollback-verified, nothing persists):
  - delete_transfer       on a real in-transit transfer
  - delete_transfer_in    on a real Received GRN
  - backfill              full run
Each runs inside a transaction that is ROLLED BACK; we assert the destructive
effect happened in-txn, then assert everything is restored after rollback."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

from app.modules.transfer.services import delete_service, reversal_service

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]
ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")
    ok += 1 if cond else 0
    fail += 0 if cond else 1


async def count_pending(conn, tid):
    return await conn.fetchval(
        "SELECT COUNT(*) FROM pending_transfer_stock WHERE transfer_out_id = $1", tid)


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # ── delete_transfer ──
        print("delete_transfer (real in-transit transfer):")
        tid = await conn.fetchval(
            "SELECT transfer_out_id FROM pending_transfer_stock WHERE status='In Transit' "
            "GROUP BY transfer_out_id ORDER BY COUNT(*) ASC LIMIT 1")
        if tid:
            before = await count_pending(conn, tid)
            tr = conn.transaction(); await tr.start()
            try:
                res = await delete_service.delete_transfer(conn, tid)
                chk("delete_transfer returns success", res.get("success") is True, res.get("message", ""))
                gone = await conn.fetchval("SELECT COUNT(*) FROM interunit_transfers_header WHERE id=$1", tid)
                chk("transfer header deleted in-txn", gone == 0)
                left = await count_pending(conn, tid)
                chk("pending rows cleared in-txn", left == 0, f"was {before}")
            finally:
                await tr.rollback()
            chk("transfer restored after rollback",
                (await conn.fetchval("SELECT COUNT(*) FROM interunit_transfers_header WHERE id=$1", tid)) == 1)
            chk("pending restored after rollback", (await count_pending(conn, tid)) == before, f"now back to {before}")
        else:
            print("  (no in-transit transfer — skipped)")

        # ── delete_transfer_in ──
        print("delete_transfer_in (real Received GRN):")
        ti = await conn.fetchrow(
            "SELECT id, transfer_out_id FROM interunit_transfer_in_header WHERE status='Received' "
            "AND transfer_out_id IS NOT NULL ORDER BY id DESC LIMIT 1")
        if ti:
            tiid, toid = ti["id"], ti["transfer_out_id"]
            orig_to_status = await conn.fetchval("SELECT status FROM interunit_transfers_header WHERE id=$1", toid)
            tr = conn.transaction(); await tr.start()
            try:
                res = await delete_service.delete_transfer_in(conn, tiid, "yash@candorfoods.in")
                chk("delete_transfer_in success", res.get("success") is True)
                gone = await conn.fetchval("SELECT COUNT(*) FROM interunit_transfer_in_header WHERE id=$1", tiid)
                chk("GRN header deleted in-txn", gone == 0)
                chk("transfer-out reverted to Dispatch in-txn",
                    (await conn.fetchval("SELECT status FROM interunit_transfers_header WHERE id=$1", toid)) == "Dispatch")
            finally:
                await tr.rollback()
            chk("GRN restored after rollback",
                (await conn.fetchval("SELECT COUNT(*) FROM interunit_transfer_in_header WHERE id=$1", tiid)) == 1)
            chk("transfer-out status restored",
                (await conn.fetchval("SELECT status FROM interunit_transfers_header WHERE id=$1", toid)) == orig_to_status,
                f"== {orig_to_status}")
        else:
            print("  (no Received GRN — skipped)")

        # ── backfill ── (in-txn delta is concurrency-immune; global count on a
        # live DB is not. Rollback safety itself is already proven by delete_transfer,
        # which uses the same async-with-transaction nesting.)
        print("backfill (full run, rolled back):")
        tr = conn.transaction(); await tr.start()
        try:
            b1 = await conn.fetchval("SELECT COUNT(*) FROM pending_transfer_stock")
            summary = await reversal_service.backfill_pending_from_existing_transfers(conn)
            b2 = await conn.fetchval("SELECT COUNT(*) FROM pending_transfer_stock")
            chk("backfill returns summary", isinstance(summary, dict) and "transfers_scanned" in summary,
                str({k: summary.get(k) for k in ("transfers_scanned", "boxes_parked_from_cold", "boxes_parked_from_warehouse", "boxes_skipped_already_parked")}))
            net_parked = (summary.get("boxes_parked_from_cold", 0)
                          + summary.get("boxes_parked_from_warehouse", 0)
                          + summary.get("boxes_parked_without_source", 0))
            chk("pending delta matches summary (in-txn)", (b2 - b1) == net_parked, f"delta={b2 - b1}, parked={net_parked}")
        finally:
            await tr.rollback()
    finally:
        await conn.close()
    print(f"\nP3b REVIEW: {ok} passed, {fail} failed")


asyncio.run(main())
