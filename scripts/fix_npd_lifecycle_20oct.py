"""One-off NPD clean-up, 20 Oct 2026 — run AFTER deploying the request-lifecycle fix.

The fix makes a request close itself when its sample is dispatched, lets a job card made
from the menu be linked to its request, and stops the overdue chase once the job card is
closed. It cannot reach back to what happened before it existed, so this script does that
once, through the same service functions the app now uses:

  1. Link job card 34302860 ("Flavoured nuts", More Retail, closed 10-Sep) to request
     33461896 — it was built from the menu for that request (same three articles), so the
     request never saw it and was chased 176 times.
  2. Close every open NPD / TRIAL request the queue already calls Dispatched: everything
     it asked for, or everything its job cards made, has gone out (11202736, 73326175 and
     40794270 on 20-Oct), but nothing existed to close them.
  3. Cancel job card 27297513 (Protein bar, IN_DEVELOPMENT since 13-Jul). Its request
     26926184 was closed by the 7-Sep bulk close; its promote request has waited 99 days
     for an inventory manager. Cancelling voids that promote request, so NO BOM is made
     from it — if that recipe should become a live BOM, have an inventory manager accept
     the promote instead and run with --skip-cancel.

Default is a dry run: read-only session, prints the plan. --apply writes everything in ONE
transaction, after saving the rows it touches to scratch/npd_fix_20oct_backup_<ts>.json.

    DATABASE_URL=<rds warehouse_db> .venv/Scripts/python scripts/fix_npd_lifecycle_20oct.py
    DATABASE_URL=<rds warehouse_db> .venv/Scripts/python scripts/fix_npd_lifecycle_20oct.py --apply
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from app.modules.sample.services import npd_dev_service as nds  # noqa: E402
from app.modules.sample.services import requisition_service as rs  # noqa: E402

load_dotenv(ROOT / ".env")
DB_URL = os.environ["DATABASE_URL"].strip().replace("postgresql+asyncpg://", "postgresql://")
APPLY = "--apply" in sys.argv
SKIP_CANCEL = "--skip-cancel" in sys.argv

LINK_JC, LINK_REQUEST_ID = 34302860, 33461896
STUCK_JC = 27297513
SYSTEM = SimpleNamespace(user_id=None, role_name="system", is_admin=True,
                         full_name="NPD clean-up 20-Oct")
CLOSE_REMARK = ("Closed by the 20-Oct clean-up: everything had already been dispatched "
                "before requests could close themselves")
CANCEL_REASON = ("Request 26926184 was closed on 7-Sep; this card's promote request had waited "
                 "since 13-Jul for an inventory manager. Cancelled by the 20-Oct NPD clean-up.")


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


async def plan(conn) -> dict:
    out = {"link": None, "close": [], "cancel": None}
    jc = await conn.fetchrow("SELECT id, title, status, source_requisition_id FROM npd_dev_job_cards WHERE id = $1", LINK_JC)
    req = await conn.fetchrow("SELECT id, request_id, status FROM sample_requisitions WHERE request_id = $1", LINK_REQUEST_ID)
    if not jc or not req:
        print(f"  1. link: SKIP — job card {LINK_JC} or request {LINK_REQUEST_ID} not found")
    elif jc["source_requisition_id"] is not None:
        print(f"  1. link: SKIP — job card {LINK_JC} is already linked (to row {jc['source_requisition_id']})")
    else:
        out["link"] = req["id"]
        print(f"  1. link: job card {LINK_JC} '{jc['title']}' ({jc['status']}) -> request {LINK_REQUEST_ID} ({req['status']})")

    rows = await conn.fetch(
        """SELECT id, request_id, status FROM sample_requisitions
            WHERE deleted_at IS NULL AND sample_type IN ('NPD','TRIAL')
              AND status = ANY($1::text[]) ORDER BY created_at""", list(rs._AUTO_CLOSE_FROM))
    for r in rows:
        b = await rs._bucket_for(conn, r["id"])
        if b and b["display_status"] == "DISPATCHED":
            out["close"].append(r["id"])
            print(f"  2. close: request {r['request_id']} ({r['status']} -> CLOSED)")
    if not out["close"]:
        print("  2. close: nothing to close")

    stuck = await conn.fetchrow("SELECT id, title, status FROM npd_dev_job_cards WHERE id = $1", STUCK_JC)
    pending = await conn.fetchval(
        "SELECT count(*) FROM npd_dev_promote_request WHERE dev_jc_id = $1 AND status = 'PENDING'", STUCK_JC)
    if SKIP_CANCEL:
        print(f"  3. cancel: SKIPPED (--skip-cancel)")
    elif not stuck or stuck["status"] not in ("DRAFT", "IN_DEVELOPMENT"):
        print(f"  3. cancel: SKIP — job card {STUCK_JC} is {stuck['status'] if stuck else 'missing'}")
    else:
        out["cancel"] = STUCK_JC
        print(f"  3. cancel: job card {STUCK_JC} '{stuck['title']}' ({stuck['status']} -> CANCELLED), "
              f"voiding {pending} pending promote request(s)")
    return out


async def backup(conn, todo: dict) -> Path:
    req_ids = sorted(set(todo["close"]) | ({todo["link"]} if todo["link"] else set()))
    jc_ids = [LINK_JC] + ([STUCK_JC] if todo["cancel"] else [])
    data = {
        "sample_requisitions": [dict(r) for r in await conn.fetch(
            "SELECT * FROM sample_requisitions WHERE id = ANY($1::bigint[])", req_ids)],
        "npd_dev_job_cards": [dict(r) for r in await conn.fetch(
            "SELECT * FROM npd_dev_job_cards WHERE id = ANY($1::bigint[])", jc_ids)],
        "npd_dev_promote_request": [dict(r) for r in await conn.fetch(
            "SELECT * FROM npd_dev_promote_request WHERE dev_jc_id = ANY($1::bigint[])", jc_ids)],
    }
    path = ROOT / "scratch" / f"npd_fix_20oct_backup_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, default=_jsonable, indent=1), encoding="utf-8")
    return path


async def main():
    u = urlparse(DB_URL)
    print(f"target: {u.hostname} / {u.path.lstrip('/')}  |  mode: {'APPLY' if APPLY else 'DRY RUN (read-only)'}")
    conn = await asyncpg.connect(DB_URL, statement_cache_size=0)
    try:
        if not APPLY:
            await conn.execute("SET default_transaction_read_only = on")
            await plan(conn)
            print("\nDry run only. Re-run with --apply to write.")
            return
        async with conn.transaction():
            todo = await plan(conn)
            print(f"\nbackup: {await backup(conn, todo)}")
            if todo["link"]:
                await nds.link_to_requisition(conn, LINK_JC, requisition_id=todo["link"], user=SYSTEM)
            for rid in todo["close"]:
                await rs.close_if_fully_dispatched(conn, rid, user=SYSTEM, remarks=CLOSE_REMARK)
            if todo["cancel"]:
                await nds.cancel_dev_job_card(conn, STUCK_JC, reason=CANCEL_REASON, user=SYSTEM)
        print("\napplied. After:")
        await conn.execute("SET default_transaction_read_only = on")
        await plan(conn)
    finally:
        await conn.close()


asyncio.run(main())
