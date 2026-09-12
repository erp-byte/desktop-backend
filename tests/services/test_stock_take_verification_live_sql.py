"""LIVE-SQL test for the end-of-day sign-off on console stock adjustments.

Everything runs inside a transaction that is ROLLED BACK; nothing is written.

    STOCKTAKE_TEST_DATABASE_URL=postgresql://.../warehouse_db \
    PYTHONPATH=. .venv/Scripts/python tests/services/test_stock_take_verification_live_sql.py

WHY THE STATE LIVES ON THE ENTRIES ROW
stocktake_transactions is append-only -- trg_stk_txn_no_update raises on any
UPDATE -- so it cannot carry a mutable verified flag. The sign-off is recorded on
the ADJUSTMENT row in new_stock_entries, which already has verified /
verified_by / verified_at, and a transaction reads its state back from that row
via the key uq_nse_adjustment_day enforces:

    (IST day, UPPER(BTRIM(item_name)), warehouse, floor_name, stock_type)

One sign-off therefore covers every posting made against that article and place
that day, which is what "the day's adjustments were verified" means.
"""
import asyncio
import os
import sys

import asyncpg

from app.config import Settings
from app.modules.stock_take.services import transactions_service as svc

ITEM = "ZZ VERIFICATION LIVE TEST"
WH = "W202"
FLOOR = "First Floor"
POSTER = "Test Poster"
VERIFIER = "Test Verifier"

_passed = 0
_failed = 0


def check(label, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS  %s" % label)
    else:
        _failed += 1
        print("  FAIL  %s %s" % (label, extra))


async def entry_state(conn, entry_id):
    return dict(await conn.fetchrow(
        "SELECT verified, verified_by, verified_at, total_weight "
        "FROM new_stock_entries WHERE id = $1", entry_id))


async def main():
    url = os.getenv("STOCKTAKE_TEST_DATABASE_URL") or Settings().DATABASE_URL
    conn = await asyncpg.connect(url)
    try:
        if not await conn.fetchval("SELECT to_regclass('new_stock_entries')"):
            print("\n  SKIP  new_stock_entries is not in the configured database.\n")
            return

        tx = conn.transaction()
        await tx.start()
        try:
            print("\n[1] A posted adjustment is NOT pre-verified")
            e1 = await svc.write_back_entry(
                conn, item_name=ITEM, stock_type="Fresh Stock", warehouse=WH,
                location=FLOOR, units_delta=5, kg_delta=20.0, actor=POSTER)
            s = await entry_state(conn, e1["entry_id"])
            check("verified is false on insert", s["verified"] is False, str(s))
            check("no verifier is recorded",
                  s["verified_by"] is None and s["verified_at"] is None, str(s))

            print("\n[2] Signing off stamps the VERIFIER, not the poster")
            res = await svc.verify_entries(
                conn, actor=VERIFIER, warehouse=WH, location=FLOOR)
            s = await entry_state(conn, e1["entry_id"])
            check("one row signed", res["verified_count"] == 1, str(res["verified_count"]))
            check("verified is true", s["verified"] is True)
            check("signed by the verifier", s["verified_by"] == VERIFIER, str(s["verified_by"]))
            check("the poster is not the verifier", s["verified_by"] != POSTER)

            print("\n[3] Signing again is a no-op, not a re-stamp")
            was_by, was_at = s["verified_by"], s["verified_at"]
            again = await svc.verify_entries(
                conn, actor="Someone Else", warehouse=WH, location=FLOOR)
            s = await entry_state(conn, e1["entry_id"])
            check("nothing re-signed", again["verified_count"] == 0, str(again["verified_count"]))
            check("the original signature stands",
                  s["verified_by"] == was_by and s["verified_at"] == was_at, str(s))

            print("\n[4] Adjusting again clears the sign-off")
            e2 = await svc.write_back_entry(
                conn, item_name=ITEM, stock_type="Fresh Stock", warehouse=WH,
                location=FLOOR, units_delta=2, kg_delta=-8.0, actor=POSTER)
            s = await entry_state(conn, e1["entry_id"])
            check("folds into the same day's row", e2["entry_id"] == e1["entry_id"])
            check("the figure moved", abs(float(s["total_weight"]) - 12.0) < 1e-9,
                  str(s["total_weight"]))
            check("verified is cleared", s["verified"] is False, str(s))
            check("the stale signature is cleared",
                  s["verified_by"] is None and s["verified_at"] is None, str(s))

            print("\n[5] A transaction reads its sign-off back from that row")
            await svc.create_transaction(
                conn,
                {"item_name": ITEM, "stock_type": "Fresh Stock", "operation": "ADDITION",
                 "units": 1, "qty_kg": 3.0, "reason": "verification live test"},
                warehouse=WH, location=FLOOR,
                created_by=POSTER, created_by_user_id=None)
            listing = await svc.list_transactions(conn, item_name=ITEM, page_size=10)
            txns = listing["transactions"]
            check("the posting is listed", len(txns) >= 1, str(len(txns)))
            check("it reads as unverified",
                  all(t["verified"] is False for t in txns))
            check("every row carries its IST business day",
                  all(t.get("business_day") for t in txns))

            print("\n[6] Signing off flows through to the transaction")
            await svc.verify_entries(conn, actor=VERIFIER, warehouse=WH, location=FLOOR)
            txns = (await svc.list_transactions(conn, item_name=ITEM, page_size=10))["transactions"]
            check("it now reads as verified", all(t["verified"] is True for t in txns))
            check("carrying the verifier's name",
                  all(t["verified_by"] == VERIFIER for t in txns))

            print("\n[7] A line with NO adjustments can still be signed")
            # Most rows on the adjust screen have never been adjusted. Restricting
            # this to source_kind='ADJUSTMENT' would leave the majority of lines
            # with nothing a reviewer could sign, which is why it covers counts.
            only_count = await conn.fetchrow("""
                SELECT id, item_name, warehouse, floor_name,
                       (created_at AT TIME ZONE 'Asia/Kolkata')::date AS d
                  FROM new_stock_entries
                 WHERE source_kind = 'COUNT' AND COALESCE(verified, FALSE) = FALSE
                   AND (created_at AT TIME ZONE 'Asia/Kolkata')::date
                       < (now() AT TIME ZONE 'Asia/Kolkata')::date
                 LIMIT 1""")
            if only_count:
                res = await svc.verify_entries(
                    conn, actor=VERIFIER, warehouse=only_count["warehouse"],
                    location=only_count["floor_name"], item_name=only_count["item_name"])
                after = await entry_state(conn, only_count["id"])
                check("a count-only line signs", res["verified_count"] >= 1,
                      str(res["verified_count"]))
                check("even though its day is not today", after["verified"] is True,
                      "day %s" % only_count["d"])
            else:
                check("a count-only line signs", True, "(none unverified to try)")

            print("\n[8] A bulk sign-off does NOT reach back through history")
            # No article named means the end-of-day button, which must stay bound
            # to today -- otherwise one click signs off every figure ever counted.
            n_today = await conn.fetchval("""
                SELECT COUNT(*) FROM new_stock_entries
                 WHERE COALESCE(verified, FALSE) = FALSE
                   AND (created_at AT TIME ZONE 'Asia/Kolkata')::date
                       = (now() AT TIME ZONE 'Asia/Kolkata')::date""")
            bulk = await svc.verify_entries(conn, actor=VERIFIER)
            check("bulk signs exactly today's unverified rows",
                  bulk["verified_count"] == n_today,
                  "signed %s, today has %s" % (bulk["verified_count"], n_today))
            left = await conn.fetchval("""
                SELECT COUNT(*) FROM new_stock_entries
                 WHERE COALESCE(verified, FALSE) = FALSE
                   AND (created_at AT TIME ZONE 'Asia/Kolkata')::date
                       < (now() AT TIME ZONE 'Asia/Kolkata')::date""")
            check("older rows are left alone", left > 0 or n_today == 0,
                  "%s older rows still unverified" % left)

            print("\n[9] The ledger itself is still append-only")
            try:
                await conn.execute(
                    "UPDATE stocktake_transactions SET reason = reason "
                    "WHERE txn_id = (SELECT MIN(txn_id) FROM stocktake_transactions)")
                check("UPDATE is blocked", False, "no raise")
            except asyncpg.PostgresError as exc:
                check("UPDATE is blocked", "append-only" in str(exc), str(exc)[:60])
        finally:
            await tx.rollback()
            print("\nrolled back — nothing written")
    finally:
        await conn.close()

    print("\n=== %d passed, %d failed ===" % (_passed, _failed))
    if _failed:
        sys.exit(1)


# Guarded, like every other live-SQL test here: without it pytest EXECUTES the
# module at collection time, and Settings() raises wherever .env is absent --
# a git worktree, CI -- turning a skippable test into a collection error that
# aborts the whole run.
if __name__ == "__main__":
    asyncio.run(main())
