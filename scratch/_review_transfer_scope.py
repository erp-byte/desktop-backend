"""READ-ONLY review of warehouse scoping on the dashboard list endpoints."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

from app.modules.transfer.services import query_service as q
from app.modules.transfer.services import pending_service, inner_cold_service

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]
ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")
    ok += 1 if cond else 0
    fail += 0 if cond else 1


def involves(rec, wh, keys):
    wl = wh.lower()
    return any((rec.get(k) or "").lower() == wl for k in keys)


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    WH = "A185"
    try:
        # Requests
        unscoped = await q.list_requests(conn, page=1, per_page=1000)
        scoped = await q.list_requests(conn, page=1, per_page=1000, scope=[WH])
        chk("requests: scoped <= unscoped", scoped["total"] <= unscoped["total"],
            f"scoped={scoped['total']} unscoped={unscoped['total']}")
        chk("requests: every record involves A185",
            all(involves(r, WH, ("from_warehouse", "to_warehouse")) for r in scoped["records"]),
            f"n={len(scoped['records'])}")
        chk("requests: unscoped ([]) returns all",
            (await q.list_requests(conn, page=1, per_page=1000, scope=[]))["total"] == unscoped["total"])
        chk("requests: scope is case-insensitive",
            (await q.list_requests(conn, page=1, per_page=1000, scope=["a185"]))["total"] == scoped["total"])
        chk("requests: hyphenated scope ('A-185') matches 'A185' data",
            (await q.list_requests(conn, page=1, per_page=1000, scope=["A-185"]))["total"] == scoped["total"])

        # Transfers
        t_un = await q.list_transfers(conn, page=1, per_page=500)
        t_sc = await q.list_transfers(conn, page=1, per_page=500, scope=[WH])
        chk("transfers: scoped <= unscoped", t_sc["total"] <= t_un["total"],
            f"scoped={t_sc['total']} unscoped={t_un['total']}")
        chk("transfers: every record involves A185 (from/to/cold)",
            all(involves(r, WH, ("from_warehouse", "to_warehouse", "from_cold_unit")) for r in t_sc["records"]),
            f"n={len(t_sc['records'])}")

        # Transfer-ins
        ti_un = await q.list_transfer_ins(conn, page=1, per_page=500)
        ti_sc = await q.list_transfer_ins(conn, page=1, per_page=500, scope=[WH])
        chk("transfer-ins: scoped <= unscoped", ti_sc["total"] <= ti_un["total"],
            f"scoped={ti_sc['total']} unscoped={ti_un['total']}")
        chk("transfer-ins: every record involves A185 (recv/from)",
            all(involves(r, WH, ("receiving_warehouse", "from_warehouse")) for r in ti_sc["records"]),
            f"n={len(ti_sc['records'])}")

        # Pending stock (In Transit card + modal)
        p_un = await pending_service.list_pending_transfers(conn)
        p_sc = await pending_service.list_pending_transfers(conn, scope=[WH])
        chk("pending: scoped <= unscoped", p_sc["total"] <= p_un["total"],
            f"scoped={p_sc['total']} unscoped={p_un['total']}")
        chk("pending: every record involves A185 (from/to site)",
            all(involves(r, WH, ("from_site", "to_site")) for r in p_sc["records"]),
            f"n={len(p_sc['records'])}")

        # Inner cold
        ic_un = await inner_cold_service.list_inner_cold(conn, page=1, per_page=500)
        ic_sc = await inner_cold_service.list_inner_cold(conn, page=1, per_page=500, scope=[WH])
        chk("inner-cold: scoped <= unscoped", ic_sc["total"] <= ic_un["total"],
            f"scoped={ic_sc['total']} unscoped={ic_un['total']}")
        chk("inner-cold: every record from A185",
            all((r.get("from_warehouse") or "").lower() == WH.lower() for r in ic_sc["records"]),
            f"n={len(ic_sc['records'])}")
    finally:
        await conn.close()
    print(f"\nSCOPE REVIEW: {ok} passed, {fail} failed")


asyncio.run(main())
