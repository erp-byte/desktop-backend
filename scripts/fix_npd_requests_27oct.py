"""NPD request clean-up, 27 Oct 2026. Supersedes fix_npd_lifecycle_20oct.py (never run).

Reminders already stop once a request's job card is closed (the 20-Oct fix is live), but
the requests themselves never closed: NPD dispatched the samples without recording the
dispatch on the job card, so the queue still shows "In process". This script, run once:

  1. Links job card 34302860 ("Flavoured nuts", More Retail) to request 33461896 — it was
     made from the menu for that request (same customer, same three articles), so the
     request had no card and was still being chased daily.
  2. Accepts request 72121415 (Coated Peanut Base, Open Secret), pending NPD review since
     21-Sep. This sends the usual "accepted by NPD" WhatsApp / mail to the business head.
  3. Closes every open request whose job cards are all closed: the business team confirmed
     on 27-Oct that those samples have been dispatched. Where the dispatch WAS recorded on
     the card (40794270, 73326175, 11202736) the normal automatic close is used.
  4. Cancels job card 27297513 (Protein bar), stuck in development since 13-Jul behind a
     promote request no inventory manager ever answered; its request 26926184 was closed
     on 7-Sep. Cancelling voids the promote request, so no BOM is made from it
     (--skip-cancel leaves it alone).

Requests with NO job card at all are left open unless named in --close-no-card, e.g.
    --close-no-card 14516534,33859648
for samples that went out without a job card. Until then the overdue reminder keeps
chasing them (it stops once a card is made and closed, or the date is moved).

Actions are recorded under the "Admin" account (user 1). The R&D sample stock of a card
whose dispatch was never recorded stays in the R&D location: closing a request does not
issue stock. Recording the dispatch on the job card does both.

Dry run by default (read-only session, prints the plan). --apply writes everything in ONE
transaction after saving the rows it touches to scratch/npd_fix_27oct_backup_<ts>.json.

    DATABASE_URL=<rds warehouse_db> .venv/Scripts/python scripts/fix_npd_requests_27oct.py
    DATABASE_URL=<rds warehouse_db> .venv/Scripts/python scripts/fix_npd_requests_27oct.py --apply
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

from app.modules.sample.services import approval_service as aps  # noqa: E402
from app.modules.sample.services import audit_service  # noqa: E402
from app.modules.sample.services import npd_dev_service as nds  # noqa: E402
from app.modules.sample.services import requisition_service as rs  # noqa: E402

load_dotenv(ROOT / ".env")
DB_URL = os.environ["DATABASE_URL"].strip().replace("postgresql+asyncpg://", "postgresql://")
APPLY = "--apply" in sys.argv
SKIP_CANCEL = "--skip-cancel" in sys.argv
NO_CARD = []
for i, a in enumerate(sys.argv):
    if a == "--close-no-card" and i + 1 < len(sys.argv):
        NO_CARD = [int(x) for x in sys.argv[i + 1].split(",") if x.strip()]

LINK_JC, LINK_REQUEST_ID = 34302860, 33461896
ACCEPT_REQUEST_ID = 72121415
STUCK_JC = 27297513
ADMIN = SimpleNamespace(user_id=1, role_name="admin", is_admin=True, full_name="Admin")
TODAY = "27-Oct-2026"
CANCEL_REASON = ("Request 26926184 was closed on 7-Sep; this card's promote request had waited "
                 "since 13-Jul for an inventory manager. Cancelled by the 27-Oct NPD clean-up.")


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


async def plan(conn) -> dict:
    out = {"link": None, "accept": None, "close_ledger": [], "close_confirmed": [], "close_no_card": [], "cancel": None}
    jc = await conn.fetchrow("SELECT id, title, status, source_requisition_id FROM npd_dev_job_cards WHERE id = $1", LINK_JC)
    req = await conn.fetchrow("SELECT id, request_id, status FROM sample_requisitions WHERE request_id = $1", LINK_REQUEST_ID)
    if jc and req and jc["source_requisition_id"] is None and req["status"] in rs.OPEN_STATUSES_EDITABLE_DATE:
        out["link"] = req["id"]
        print(f"  1. link    job card {LINK_JC} '{jc['title']}' -> request {LINK_REQUEST_ID}")
    else:
        print(f"  1. link    SKIP (card {LINK_JC} already linked, or request {LINK_REQUEST_ID} not open)")

    acc = await conn.fetchrow("SELECT id, status FROM sample_requisitions WHERE request_id = $1", ACCEPT_REQUEST_ID)
    if acc and acc["status"] in ("SUBMITTED", "ON_HOLD"):
        out["accept"] = acc["id"]
        print(f"  2. accept  request {ACCEPT_REQUEST_ID} ({acc['status']} -> BH_APPROVED, as Admin)")
    else:
        print(f"  2. accept  SKIP (request {ACCEPT_REQUEST_ID} is {acc['status'] if acc else 'missing'})")

    rows = await conn.fetch(
        """SELECT sr.id, sr.request_id, sr.status, sr.company_name,
                  array_agg(jc.id ORDER BY jc.id) FILTER (WHERE jc.status <> 'CANCELLED') AS cards,
                  bool_and(jc.status = 'CLOSED') FILTER (WHERE jc.status <> 'CANCELLED') AS all_closed
             FROM sample_requisitions sr
             LEFT JOIN npd_dev_job_cards jc
               ON jc.source_requisition_id = sr.id OR jc.id = sr.linked_dev_jc_id
                  OR (jc.id = $2 AND sr.id = $3)          -- the card step 1 links
            WHERE sr.deleted_at IS NULL AND sr.sample_type IN ('NPD','TRIAL')
              AND sr.status = ANY($1::text[])
            GROUP BY sr.id, sr.request_id, sr.status, sr.company_name, sr.created_at
            ORDER BY sr.created_at""",
        list(rs._AUTO_CLOSE_FROM), LINK_JC, out["link"] or -1)
    for r in rows:
        cards = [c for c in (r["cards"] or []) if c is not None]
        if cards and r["all_closed"]:
            b = await rs._bucket_for(conn, r["id"])
            key = "close_ledger" if b and b["display_status"] == "DISPATCHED" else "close_confirmed"
            out[key].append((r["id"], r["request_id"], cards))
            how = "dispatch recorded on the card" if key == "close_ledger" else "dispatch confirmed by the business team"
            print(f"  3. close   request {r['request_id']:<9} {r['company_name'] or '':<28} cards {cards}  ({how})")
        elif not cards and r["request_id"] in NO_CARD:
            out["close_no_card"].append((r["id"], r["request_id"], []))
            print(f"  3. close   request {r['request_id']:<9} {r['company_name'] or '':<28} no job card  (--close-no-card)")
    left = [r["request_id"] for r in rows if not [c for c in (r["cards"] or []) if c is not None] and r["request_id"] not in NO_CARD]
    if out["accept"]:
        left.append(ACCEPT_REQUEST_ID)
    print(f"     left open (no job card): {left}")

    stuck = await conn.fetchrow("SELECT id, title, status FROM npd_dev_job_cards WHERE id = $1", STUCK_JC)
    if SKIP_CANCEL:
        print("  4. cancel  SKIPPED (--skip-cancel)")
    elif stuck and stuck["status"] in ("DRAFT", "IN_DEVELOPMENT"):
        out["cancel"] = STUCK_JC
        print(f"  4. cancel  job card {STUCK_JC} '{stuck['title']}' ({stuck['status']} -> CANCELLED, voids its promote request)")
    else:
        print(f"  4. cancel  SKIP (job card {STUCK_JC} is {stuck['status'] if stuck else 'missing'})")
    return out


async def close_confirmed(conn, req_id: int, cards: list[int], *, no_card: bool) -> None:
    where = ("it went out without a job card" if no_card
             else f"the dispatch was not recorded on job card {', '.join(map(str, cards))}")
    remark = f"Closed: the business team confirmed on {TODAY} that the sample was dispatched ({where})"
    prev = await conn.fetchval("SELECT status FROM sample_requisitions WHERE id = $1", req_id)
    res = await conn.execute(
        """UPDATE sample_requisitions SET status = 'CLOSED', updated_at = NOW(), updated_by = $2
            WHERE id = $1 AND status = ANY($3::text[])""", req_id, ADMIN.user_id, list(rs._AUTO_CLOSE_FROM))
    if res == "UPDATE 1":
        await audit_service.write_audit(
            conn, req_id, audit_service.EV_STATUS_CHANGE, old_value={"status": prev},
            new_value={"status": "CLOSED"}, actor_user_id=ADMIN.user_id, actor_role=ADMIN.role_name,
            remarks=remark)


async def backup(conn, todo: dict) -> Path:
    req_ids = {x[0] for k in ("close_ledger", "close_confirmed", "close_no_card") for x in todo[k]}
    req_ids |= {v for v in (todo["link"], todo["accept"]) if v}
    jc_ids = [LINK_JC, STUCK_JC]
    data = {
        "sample_requisitions": [dict(r) for r in await conn.fetch(
            "SELECT * FROM sample_requisitions WHERE id = ANY($1::bigint[])", sorted(req_ids))],
        "npd_dev_job_cards": [dict(r) for r in await conn.fetch(
            "SELECT * FROM npd_dev_job_cards WHERE id = ANY($1::bigint[])", jc_ids)],
        "npd_dev_promote_request": [dict(r) for r in await conn.fetch(
            "SELECT * FROM npd_dev_promote_request WHERE dev_jc_id = ANY($1::bigint[])", jc_ids)],
    }
    path = ROOT / "scratch" / f"npd_fix_27oct_backup_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
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
                await nds.link_to_requisition(conn, LINK_JC, requisition_id=todo["link"], user=ADMIN)
            for req_id, rid, _cards in todo["close_ledger"]:
                await rs.close_if_fully_dispatched(
                    conn, req_id, user=ADMIN,
                    remarks="Closed: everything had already been dispatched (recorded on the job card)")
            for req_id, _rid, cards in todo["close_confirmed"]:
                await close_confirmed(conn, req_id, cards, no_card=False)
            for req_id, _rid, cards in todo["close_no_card"]:
                await close_confirmed(conn, req_id, cards, no_card=True)
            if todo["cancel"]:
                await nds.cancel_dev_job_card(conn, STUCK_JC, reason=CANCEL_REASON, user=ADMIN)
            # Last, so a failure above rolls back before any message goes out.
            if todo["accept"]:
                await aps.act_npd_review(conn, todo["accept"], action="ACCEPT", user=ADMIN)
        print("\napplied. Now:")
        await conn.execute("SET default_transaction_read_only = on")
        await plan(conn)
    finally:
        await conn.close()


asyncio.run(main())
