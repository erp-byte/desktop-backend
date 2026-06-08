"""READ-ONLY P2 review harness: execute each ported read query against the live
DB and validate the result against its Pydantic response schema. All SELECTs."""
import asyncio
import os
import traceback

import asyncpg
from dotenv import load_dotenv

from app.modules.transfer import schemas
from app.modules.transfer.services import query_service as q

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    ok, fail = 0, 0

    def check(name, fn):
        nonlocal ok, fail
        try:
            fn()
            print(f"  PASS  {name}")
            ok += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
            fail += 1

    try:
        # ── Requests ──
        r = await q.list_requests(conn, page=1, per_page=5)
        check("list_requests schema", lambda: schemas.RequestListResponse.model_validate(r))
        print(f"        total={r['total']} returned={len(r['records'])}")
        if r["records"]:
            rid = r["records"][0]["id"]
            gr = await q.get_request(conn, rid)
            check(f"get_request({rid}) schema", lambda: schemas.RequestWithLines.model_validate(gr))

        # ── Transfers OUT ──
        t = await q.list_transfers(conn, page=1, per_page=5, sort_by="created_ts", sort_order="desc")
        check("list_transfers schema", lambda: schemas.TransferListResponse.model_validate(t))
        print(f"        total={t['total']} returned={len(t['records'])}")
        if t["records"]:
            tid = t["records"][0]["id"]
            gt = await q.get_transfer(conn, tid)
            check(f"get_transfer({tid}) schema", lambda: schemas.TransferWithLines.model_validate(gt))
            print(f"        transfer {tid}: lines={len(gt['lines'])} boxes={len(gt['boxes'])} grn={len(gt['grn_records'])}")

        # ── Transfers IN ──
        ti = await q.list_transfer_ins(conn, page=1, per_page=5, sort_by="created_at", sort_order="desc")
        check("list_transfer_ins schema", lambda: schemas.TransferInListResponse.model_validate(ti))
        print(f"        total={ti['total']} returned={len(ti['records'])}")
        if ti["records"]:
            tiid = ti["records"][0]["id"]
            gti = await q.get_transfer_in(conn, tiid)
            check(f"get_transfer_in({tiid}) schema", lambda: schemas.TransferInDetail.model_validate(gti))
            print(f"        transfer-in {tiid}: boxes={len(gti['boxes'])}")

        # ── filter / sort smoke ──
        await q.list_transfers(conn, page=1, per_page=3, status="Dispatch")
        await q.list_transfers(conn, page=1, per_page=3, from_site="Rishi")  # cold-unit normalization path
        check("list_transfers filters run", lambda: True)
    finally:
        await conn.close()
    print(f"\nP2 REVIEW: {ok} passed, {fail} failed")


asyncio.run(main())
