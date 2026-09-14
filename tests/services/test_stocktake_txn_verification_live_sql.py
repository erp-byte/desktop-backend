"""LIVE-SQL test for per-transaction sign-off and its two reconciliation rules.

EVERYTHING RUNS INSIDE A TRANSACTION THAT IS ROLLED BACK, including 110 itself --
DDL is transactional in Postgres, so this exercises the real migration against the
real database without changing it. Deliberate-failure cases use savepoints,
because Postgres aborts the whole transaction on the first error.

THE INVARIANT UNDER TEST, in both directions:

    an adjustment row in new_stock_entries is verified
      <=>  every transaction in its (IST day, article, warehouse, floor,
           stock type) group is verified

  rule 1  verify the last unsigned posting  -> the line signs itself off
          un-verify any one posting         -> the line's signature comes off
  rule 2  verify the line                   -> every posting behind it signs off
          un-verify the line                -> every posting un-signs

AND THE HALF THAT MUST NOT CHANGE: the ledger is still append-only. 110 narrows
trg_stk_txn_no_update to the three verification columns rather than removing it,
so an UPDATE touching qty_kg, item_name or anything else must still raise. If
that regresses, this file fails on `the posting itself is still frozen`.

THE EMPTY GROUP is the trap rule 1 has to dodge: "every transaction is verified"
is vacuously true of a line with no transactions, and most lines have none.

Run against the database that actually holds the tables:
    STOCKTAKE_TEST_DATABASE_URL=postgresql://.../warehouse_db \
    PYTHONPATH=. .venv/Scripts/python tests/services/test_stocktake_txn_verification_live_sql.py
"""
import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from app.config import Settings
from app.modules.stock_take.services import transactions_service as svc

DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"
MIGRATIONS = [DB_DIR / "110_stocktake_txn_verification.sql"]

IST = timezone(timedelta(hours=5, minutes=30))
ITEM = "ZZ TEST ARTICLE FOR TXN VERIFICATION"
WH, FLOOR, STOCK = "A185", "A185 Cold", "Fresh Stock"
POSTER, VERIFIER = "Test Poster", "Test Verifier"

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


def migration_sql(path: Path) -> str:
    """One migration with its BEGIN/COMMIT stripped.

    Executed inside this test's transaction, that COMMIT would commit the
    fixtures too -- the one thing this file must never do. Matched as a
    STATEMENT, not as a substring: the files' own prose says "released on commit
    or rollback", and a substring test on that reports a failure that is not one.
    """
    sql = path.read_text(encoding="utf-8")
    sql = "\n".join(
        ln for ln in sql.splitlines()
        if not re.match(r"^\s*(COMMIT|BEGIN)\s*;", ln, re.I))
    assert not re.search(r"^\s*(COMMIT|BEGIN)\s*;", sql, re.I | re.M), path.name
    return sql


async def post(conn, qty=1.0, operation="ADDITION"):
    """One adjustment through the real service. Returns its txn_id."""
    r = await svc.create_transaction(
        conn,
        {"item_name": ITEM, "sku_id": None, "is_new_article": True,
         "material_type": "rm", "item_category": "test", "item_subcategory": "test",
         "stock_type": STOCK, "units": qty, "qty_kg": qty,
         "operation": operation, "reason": "verification fixture"},
        warehouse=WH, location=FLOOR, created_by=POSTER, created_by_user_id=None)
    return r["transaction"]["txn_id"]


async def entry_state(conn):
    """The adjustment row for today's fixture group, or None."""
    return await conn.fetchrow(
        f"""
        SELECT id, verified, verified_by, verified_at
          FROM {svc.ENTRIES_TABLE}
         WHERE source_kind = 'ADJUSTMENT'
           AND (created_at AT TIME ZONE 'Asia/Kolkata')::date
               = (now() AT TIME ZONE 'Asia/Kolkata')::date
           AND UPPER(BTRIM(item_name)) = $1
        """, ITEM.upper())


async def txn_states(conn, ids):
    rows = await conn.fetch(
        "SELECT txn_id, verified, verified_by FROM stocktake_transactions "
        "WHERE txn_id = ANY($1::bigint[]) ORDER BY txn_id", list(ids))
    return {r["txn_id"]: r for r in rows}


async def main() -> int:
    dsn = os.getenv("STOCKTAKE_TEST_DATABASE_URL") or Settings().DATABASE_URL
    conn = await asyncpg.connect(dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        live = await conn.fetchval(
            "SELECT COUNT(*) FROM stocktake_transactions WHERE UPPER(BTRIM(item_name)) = $1",
            ITEM.upper())
        assert live == 0, "fixture article already has %s real row(s)" % live

        print("\nAPPLYING 110 (inside this transaction)")
        for m in MIGRATIONS:
            await conn.execute(migration_sql(m))
        check("migration applied", True)

        cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'stocktake_transactions'")}
        check("ledger carries verified / verified_by / verified_at",
              {"verified", "verified_by", "verified_at"} <= cols,
              str(sorted(cols & {"verified", "verified_by", "verified_at"})))

        print("\nTHE LEDGER IS STILL APPEND-ONLY")
        await conn.execute("SAVEPOINT s_frozen")
        t0 = await post(conn)
        for col, val in (("qty_kg", "99"), ("item_name", "'HACKED'"),
                         ("warehouse", "'W202'"), ("reason", "'changed'")):
            await conn.execute("SAVEPOINT s_col")
            try:
                await conn.execute(
                    f"UPDATE stocktake_transactions SET {col} = {val} WHERE txn_id = $1", t0)
                check("the posting itself is still frozen (%s)" % col, False,
                      "UPDATE succeeded")
            except asyncpg.PostgresError as exc:
                check("the posting itself is still frozen (%s)" % col,
                      "append-only" in str(exc), str(exc)[:70])
            await conn.execute("ROLLBACK TO SAVEPOINT s_col")
        await conn.execute("SAVEPOINT s_del")
        try:
            await conn.execute("DELETE FROM stocktake_transactions WHERE txn_id = $1", t0)
            check("DELETE is still blocked outright", False, "DELETE succeeded")
        except asyncpg.PostgresError as exc:
            check("DELETE is still blocked outright", "append-only" in str(exc),
                  str(exc)[:70])
        await conn.execute("ROLLBACK TO SAVEPOINT s_del")
        await conn.execute("ROLLBACK TO SAVEPOINT s_frozen")

        print("\nA POSTING IS BORN UNSIGNED")
        await conn.execute("SAVEPOINT s_rule1")
        a = await post(conn)
        b = await post(conn)
        st = await txn_states(conn, [a, b])
        check("both postings start unverified",
              not st[a]["verified"] and not st[b]["verified"])
        e = await entry_state(conn)
        check("and so does their line", e is not None and not e["verified"])

        print("\nRULE 1 -- the line follows its postings")
        out = await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[a])
        check("one posting changed", out["changed"] == 1, str(out["changed"]))
        e = await entry_state(conn)
        check("line NOT signed while one posting is unsigned", not e["verified"])
        check("no line was reconciled yet", out["entries_reconciled"] == 0,
              str(out["entries_reconciled"]))

        out = await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[b])
        e = await entry_state(conn)
        check("signing the LAST posting signs the line off", e["verified"], str(dict(e)))
        check("...naming the verifier", e["verified_by"] == VERIFIER, str(e["verified_by"]))
        check("...and the poster is not the verifier", e["verified_by"] != POSTER)
        check("the call reports the line it moved", out["entries_reconciled"] == 1,
              str(out["entries_reconciled"]))

        print("\nRULE 1 IN REVERSE -- un-signing one posting un-signs the line")
        out = await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[a], verified=False)
        e = await entry_state(conn)
        check("line's signature comes back off", not e["verified"], str(dict(e)))
        check("...and its name goes with it", e["verified_by"] is None, str(e["verified_by"]))
        check("the other posting is untouched", (await txn_states(conn, [b]))[b]["verified"])

        print("\nRE-TICKING IS A NO-OP, NOT A RE-STAMP")
        again = await svc.verify_transactions(conn, actor="Someone Else", txn_ids=[b])
        check("already-signed posting reports no change", again["changed"] == 0,
              str(again["changed"]))
        check("...and keeps its original signature",
              (await txn_states(conn, [b]))[b]["verified_by"] == VERIFIER)
        await conn.execute("ROLLBACK TO SAVEPOINT s_rule1")

        print("\nRULE 2 -- the postings follow their line")
        await conn.execute("SAVEPOINT s_rule2")
        c = await post(conn)
        d = await post(conn)
        e = await entry_state(conn)
        res = await svc.verify_entries(conn, actor=VERIFIER, entry_ids=[e["id"]])
        check("the line signed off", res["verified_count"] == 1, str(res["verified_count"]))
        check("both postings cascaded", res["transactions_cascaded"] == 2,
              str(res["transactions_cascaded"]))
        st = await txn_states(conn, [c, d])
        check("...and both now read verified",
              st[c]["verified"] and st[d]["verified"])
        check("...signed by the verifier, not the poster",
              st[c]["verified_by"] == VERIFIER and st[d]["verified_by"] == VERIFIER)

        print("\nRULE 2 IN REVERSE")
        res = await svc.verify_entries(
            conn, actor=VERIFIER, entry_ids=[e["id"]], verified=False)
        check("the line un-signed", res["verified_count"] == 1)
        check("both postings cascaded back", res["transactions_cascaded"] == 2,
              str(res["transactions_cascaded"]))
        st = await txn_states(conn, [c, d])
        check("...and neither keeps a signature",
              st[c]["verified_by"] is None and st[d]["verified_by"] is None)
        await conn.execute("ROLLBACK TO SAVEPOINT s_rule2")

        print("\nA NEW POSTING UN-SIGNS THE LINE (write_back_entry, unchanged)")
        await conn.execute("SAVEPOINT s_newpost")
        f = await post(conn)
        await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[f])
        check("line signed", (await entry_state(conn))["verified"])
        g = await post(conn)
        e = await entry_state(conn)
        check("posting again takes the line's signature off", not e["verified"],
              str(dict(e)))
        st = await txn_states(conn, [f, g])
        check("...the new posting is unsigned", not st[g]["verified"])
        check("...the old one keeps its own signature", st[f]["verified"])
        check("signing the new one restores the line",
              (await svc.verify_transactions(
                  conn, actor=VERIFIER, txn_ids=[g]))["entries_reconciled"] == 1)
        check("...line verified again", (await entry_state(conn))["verified"])
        await conn.execute("ROLLBACK TO SAVEPOINT s_newpost")

        print("\nREVERSALS COUNT LIKE ANY OTHER POSTING")
        await conn.execute("SAVEPOINT s_rev")
        h = await post(conn)
        await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[h])
        rev = await svc.create_transaction(
            conn,
            {"item_name": ITEM, "sku_id": None, "is_new_article": False,
             "material_type": "rm", "item_category": "test", "item_subcategory": "test",
             "stock_type": STOCK, "units": 1, "qty_kg": 1,
             "operation": "SUBTRACTION", "reason": "reversal fixture",
             "reverses_txn_id": h},
            warehouse=WH, location=FLOOR, created_by=POSTER, created_by_user_id=None)
        rid = rev["transaction"]["txn_id"]
        check("the reversal is its own unsigned posting",
              not (await txn_states(conn, [rid]))[rid]["verified"])
        check("...so the line is unsigned too", not (await entry_state(conn))["verified"])
        await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[rid])
        check("signing the reversal restores the line",
              (await entry_state(conn))["verified"])
        await conn.execute("ROLLBACK TO SAVEPOINT s_rev")

        print("\nTHE EMPTY GROUP -- a line with no postings is never auto-signed")
        await conn.execute("SAVEPOINT s_empty")
        before = await conn.fetchval(
            f"SELECT COUNT(*) FROM {svc.ENTRIES_TABLE} "
            "WHERE source_kind = 'COUNT' AND COALESCE(verified, FALSE)")
        moved = await svc.reconcile_entry_from_transactions(
            conn, actor=VERIFIER,
            groups=[{"k_day": datetime.now(IST).date(), "k_item": "NO SUCH ARTICLE",
                     "k_wh": "A185", "k_fl": "A185 COLD", "k_stock": STOCK}])
        check("reconciling a group with no postings moves nothing", moved == 0, str(moved))
        after = await conn.fetchval(
            f"SELECT COUNT(*) FROM {svc.ENTRIES_TABLE} "
            "WHERE source_kind = 'COUNT' AND COALESCE(verified, FALSE)")
        check("...and signs off no count rows", before == after, "%s -> %s" % (before, after))
        await conn.execute("ROLLBACK TO SAVEPOINT s_empty")

        print("\nTHE LEDGER READS CARRY THE SIGN-OFF")
        await conn.execute("SAVEPOINT s_read")
        i = await post(conn)
        await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[i])
        page = await svc.list_transactions(conn, item_name=ITEM)
        row = next((t for t in page["transactions"] if t["txn_id"] == i), None)
        check("list_transactions returns the posting", row is not None)
        if row:
            check("...with verified on it", row["verified"] is True, str(row.get("verified")))
            check("...and verified_by", row["verified_by"] == VERIFIER, str(row.get("verified_by")))
            check("...verified_at as an ISO string",
                  isinstance(row["verified_at"], str), repr(row.get("verified_at")))
        exported, _ = await svc.export_transactions(conn, item_name=ITEM)
        erow = next((t for t in exported if t["txn_id"] == i), None)
        check("export_transactions carries it too",
              erow is not None and erow["verified"] is True)
        await conn.execute("ROLLBACK TO SAVEPOINT s_read")

        print("\n098 RUNNING AFTER 110 MUST NOT RE-BLOCK THE SIGN-OFF")
        # migrate.py executes every file in order on every run, and 098 sits
        # above 110 and unconditionally CREATE OR REPLACEs its hard-block
        # function. If 110 had narrowed that same function in place, this would
        # put the table back to refusing every sign-off -- permanently, if a run
        # then failed anywhere between the two files. 110 instead owns its own
        # function and re-points the triggers, which is what this proves.
        await conn.execute("SAVEPOINT s_reapply")
        await conn.execute(migration_sql(DB_DIR / "098_stocktake_transactions.sql"))
        j = await post(conn)
        try:
            out = await svc.verify_transactions(conn, actor=VERIFIER, txn_ids=[j])
            check("sign-off still works after 098 re-runs", out["changed"] == 1,
                  str(out))
        except asyncpg.PostgresError as exc:
            check("sign-off still works after 098 re-runs", False, str(exc)[:90])
        await conn.execute("SAVEPOINT s_still_frozen")
        try:
            await conn.execute(
                "UPDATE stocktake_transactions SET qty_kg = qty_kg + 1 WHERE txn_id = $1", j)
            check("...and the posting is still frozen", False, "UPDATE succeeded")
        except asyncpg.PostgresError as exc:
            check("...and the posting is still frozen", "append-only" in str(exc),
                  str(exc)[:70])
        await conn.execute("ROLLBACK TO SAVEPOINT s_still_frozen")
        fn = await conn.fetchval(
            """
            SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
             WHERE t.tgname = 'trg_stk_txn_no_update'
               AND t.tgrelid = 'stocktake_transactions'::regclass
            """)
        check("the trigger points at the narrowed guard",
              fn == "stocktake_txn_guard_write", str(fn))
        await conn.execute("ROLLBACK TO SAVEPOINT s_reapply")

        print("\nTHE BACKFILL PRESERVED WHAT WAS ALREADY ON SCREEN")
        n_verified = await conn.fetchval(
            "SELECT COUNT(*) FROM stocktake_transactions WHERE verified")
        total = await conn.fetchval("SELECT COUNT(*) FROM stocktake_transactions")
        check("real rows inherited a sign-off from their adjustment row",
              n_verified > 0, "%s of %s" % (n_verified, total))
        bad = await conn.fetchval(
            "SELECT COUNT(*) FROM stocktake_transactions "
            "WHERE (verified AND (verified_by IS NULL OR verified_at IS NULL)) "
            "   OR (NOT verified AND (verified_by IS NOT NULL OR verified_at IS NOT NULL))")
        check("every row satisfies the consistency CHECK", bad == 0, str(bad))
    finally:
        await tx.rollback()
        await conn.close()

    print("\n%d passed, %d failed" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
