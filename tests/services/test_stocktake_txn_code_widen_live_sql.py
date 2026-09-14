"""LIVE-SQL test for 109_stocktake_txn_code_widen.sql and the write path's
database-error handling.

EVERYTHING RUNS INSIDE A TRANSACTION THAT IS ROLLED BACK, including the
migration itself -- DDL is transactional in Postgres, so this exercises the real
109 against the real database without changing it. `stocktake_transactions`
blocks DELETE by trigger, so a committed test row could never be removed.
Deliberate-failure cases use savepoints, because Postgres aborts the whole
transaction on the first error.

WHAT IT GUARDS
On 2026-09-13 the day's codes ran 26256001 .. 26256999 and the next post raised
"has reached 999 adjustments". The router caught only ValueError, so the operator
saw "Internal server error" and stopped for the night. Two things had to change:
the counter had to gain a digit, and a database RAISE had to stop arriving as a
bare 500. Both are asserted here.

THE SUBTLE ONE is the mixed-width day. 100's generator read the counter with
SUBSTRING(txn_code FROM 6 FOR 3); once a day holds 262561000, that expression
returns 100 and the next insert is handed a number already in use. 109 reads to
end-of-string instead, and test_mixed_width_day fails if that regresses.

Run against the database that actually holds the table:
    STOCKTAKE_TEST_DATABASE_URL=postgresql://.../warehouse_db \
    PYTHONPATH=. .venv/Scripts/python tests/services/test_stocktake_txn_code_widen_live_sql.py
"""
import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from app.config import Settings
from app.modules.stock_take import router as st_router

MIGRATION = Path(__file__).resolve().parents[2] / "app" / "db" / "109_stocktake_txn_code_widen.sql"

# The last day of 2026, which holds no real rows -- every stock-take row is from
# February to September. SCRATCH_DAY must be the YYDDD the trigger derives from
# SCRATCH_TS or the fixtures and the minted codes would be for different days and
# nothing here would mean anything: 2026-12-31 is day 365 (2026 is not a leap
# year), so the two are asserted against each other in main() before any test runs.
IST = timezone(timedelta(hours=5, minutes=30))
SCRATCH_DAY = "26365"
SCRATCH_TS = datetime(2026, 12, 31, 12, 0, tzinfo=IST)

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


def migration_sql() -> str:
    """109 with its BEGIN/COMMIT stripped.

    The file is a standalone migration and ends in COMMIT. Executed inside this
    test's transaction that COMMIT would commit the fixtures too -- the one thing
    this file must never do -- so it is removed explicitly and the removal is
    asserted rather than assumed.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    stripped = re.sub(r"^\s*BEGIN\s*;", "", sql, count=1, flags=re.I | re.M)
    stripped = re.sub(r"^\s*COMMIT\s*;\s*$", "", stripped, count=1, flags=re.I | re.M)
    # A STATEMENT, not the word. The file's own comments say "released on commit
    # or rollback", and a substring test on that reports a failure that is not one.
    leftover = [ln for ln in stripped.splitlines()
                if re.match(r"^\s*(COMMIT|BEGIN)\s*;", ln, re.I)]
    assert not leftover, "failed to strip transaction control from 109: %r" % leftover
    return stripped


async def fill(conn, day_part: str, lo: int, hi: int, width: int) -> None:
    """Rows numbered lo..hi for `day_part`, with a `width`-digit counter."""
    await conn.execute(
        """
        INSERT INTO stocktake_transactions
            (item_name, material_type, item_category, item_subcategory, stock_type,
             units, qty_kg, operation, reason, warehouse, location, created_by,
             created_at, txn_code)
        SELECT 'txn code fixture', 'rm', 'seasoning', 'seasoning', 'Fresh Stock',
               1, 1, 'ADDITION', 'fixture', 'A185', 'A185 Cold', 'test',
               $1, $2 || lpad(g::text, $3::int, '0')
          FROM generate_series($4::int, $5::int) g
        """, SCRATCH_TS, day_part, width, lo, hi)


async def post(conn, ts):
    """One insert that lets the trigger mint the code. Returns it."""
    return await conn.fetchval(
        """
        INSERT INTO stocktake_transactions
            (item_name, material_type, item_category, item_subcategory, stock_type,
             units, qty_kg, operation, reason, warehouse, location, created_by,
             created_at)
        VALUES ('txn code fixture','rm','seasoning','seasoning','Fresh Stock',
                1,1,'ADDITION','fixture','A185','A185 Cold','test',$1)
        RETURNING txn_code
        """, ts)


async def main() -> int:
    dsn = os.getenv("STOCKTAKE_TEST_DATABASE_URL") or Settings().DATABASE_URL
    conn = await asyncpg.connect(dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        derived = await conn.fetchval(
            "SELECT to_char($1::timestamptz AT TIME ZONE 'Asia/Kolkata', 'YYDDD')",
            SCRATCH_TS)
        assert derived == SCRATCH_DAY, (
            "SCRATCH_TS is day %r but SCRATCH_DAY is %r" % (derived, SCRATCH_DAY))
        live = await conn.fetchval(
            "SELECT COUNT(*) FROM stocktake_transactions WHERE txn_code LIKE $1 || '%'",
            SCRATCH_DAY)
        assert live == 0, "scratch day %s holds %s real row(s)" % (SCRATCH_DAY, live)
        print("\nBEFORE 109 -- the state that produced the outage")
        # Reproduce 2026-09-13: a full day of 3-digit codes, then one more post.
        await conn.execute("SAVEPOINT s_before")
        await fill(conn, SCRATCH_DAY, 1, 999, 3)
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM stocktake_transactions WHERE txn_code LIKE $1 || '%'",
            SCRATCH_DAY)
        check("999 three-digit codes in place", n == 999, "got %s" % n)
        try:
            code = await post(conn, SCRATCH_TS)
            check("post #1000 refused before 109", False, "minted %s" % code)
        except asyncpg.PostgresError as exc:
            check("post #1000 refused before 109",
                  "exhausted" in str(exc), str(exc)[:90])
            check("...and it was a bare PL/pgSQL RAISE (P0001), so nothing could "
                  "tell it apart", getattr(exc, "sqlstate", None) == "P0001",
                  "sqlstate=%s" % getattr(exc, "sqlstate", None))
        await conn.execute("ROLLBACK TO SAVEPOINT s_before")

        print("\nAPPLYING 109 (inside this transaction)")
        await conn.execute(migration_sql())
        check("migration applied", True)

        print("\nAFTER 109 -- a fresh day")
        await conn.execute("SAVEPOINT s_fresh")
        code = await post(conn, SCRATCH_TS)
        check("first code of a day is 9 chars", len(code) == 9, "got %r" % code)
        check("first code counter is 0001", code == SCRATCH_DAY + "0001", "got %r" % code)
        code2 = await post(conn, SCRATCH_TS)
        check("second code increments", code2 == SCRATCH_DAY + "0002", "got %r" % code2)
        await conn.execute("ROLLBACK TO SAVEPOINT s_fresh")

        print("\nAFTER 109 -- the day that had already run out")
        await conn.execute("SAVEPOINT s_carry")
        await fill(conn, SCRATCH_DAY, 1, 999, 3)
        code = await post(conn, SCRATCH_TS)
        check("post #1000 now succeeds", code == SCRATCH_DAY + "1000", "got %r" % code)
        check("...and is 9 characters", len(code) == 9, "got %r" % code)
        await conn.execute("ROLLBACK TO SAVEPOINT s_carry")

        print("\nMIXED-WIDTH DAY -- the regression 'FOR 3' would cause")
        await conn.execute("SAVEPOINT s_mixed")
        await fill(conn, SCRATCH_DAY, 1, 999, 3)     # 8-char codes
        await fill(conn, SCRATCH_DAY, 1000, 1005, 4)  # 9-char codes
        code = await post(conn, SCRATCH_TS)
        # SUBSTRING(FROM 6 FOR 3) over '263651005' yields 100, so the old
        # expression would mint ...0101 -- a number the day already holds.
        check("counter read to end-of-string, not FOR 3",
              code == SCRATCH_DAY + "1006", "got %r" % code)
        dupes = await conn.fetchval(
            "SELECT COUNT(*) FROM stocktake_transactions WHERE txn_code = $1", code)
        check("the minted code is not already in use", dupes == 1, "count=%s" % dupes)
        await conn.execute("ROLLBACK TO SAVEPOINT s_mixed")

        print("\nTHE NEW CEILING")
        await conn.execute("SAVEPOINT s_ceiling")
        await fill(conn, SCRATCH_DAY, 1, 9999, 4)
        try:
            code = await post(conn, SCRATCH_TS)
            check("post #10000 refused", False, "minted %s" % code)
        except asyncpg.PostgresError as exc:
            check("post #10000 refused", "exhausted" in str(exc), str(exc)[:90])
            check("...with SQLSTATE 2200H, so the API can name it",
                  getattr(exc, "sqlstate", None) == "2200H",
                  "sqlstate=%s" % getattr(exc, "sqlstate", None))
            err = st_router._db_failure(exc, "create_transaction", item="x")
            check("router maps it to 409, not 500", err.status_code == 409,
                  "status=%s" % err.status_code)
            check("router names the error", err.detail["error"] == "txn_code_exhausted",
                  err.detail.get("error"))
            check("operator is told it is not their fault",
                  "Nothing you entered is wrong" in err.detail["message"],
                  err.detail["message"][:80])
            check("the database's own words are kept for whoever fixes it",
                  "exhausted" in err.detail["details"]["db_message"])
        await conn.execute("ROLLBACK TO SAVEPOINT s_ceiling")

        print("\nSHAPE CHECK ADMITS BOTH WIDTHS")
        shape = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'stk_txn_code_shape'")
        check("CHECK allows 8 and 9 digits", "{8,9}" in (shape or ""), repr(shape))
        legacy = await conn.fetchval(
            "SELECT COUNT(*) FROM stocktake_transactions WHERE LENGTH(txn_code) = 8")
        check("every existing 8-char code still validates", legacy > 0,
              "found %s" % legacy)

        print("\nEXISTING CODES ARE NOT RENUMBERED")
        first = await conn.fetchval(
            "SELECT txn_code FROM stocktake_transactions "
            "WHERE txn_code LIKE '26257%' ORDER BY txn_code LIMIT 1")
        check("26257001 is still 26257001", first == "26257001", "got %r" % first)

        print("\nOTHER DATABASE ERRORS ARE NAMED TOO")
        await conn.execute("SAVEPOINT s_fk")
        try:
            await conn.execute(
                """
                INSERT INTO stocktake_transactions
                    (item_name, sku_id, material_type, item_category, item_subcategory,
                     stock_type, units, qty_kg, operation, reason, warehouse, location,
                     created_by)
                VALUES ('fixture', 999999999, 'rm','seasoning','seasoning','Fresh Stock',
                        1,1,'ADDITION','fixture','A185','A185 Cold','test')
                """)
            check("unknown sku_id refused", False, "insert succeeded")
        except asyncpg.PostgresError as exc:
            err = st_router._db_failure(exc, "create_transaction", item="x")
            check("unknown sku_id refused", True)
            check("...mapped to 400, not 500", err.status_code == 400,
                  "status=%s" % err.status_code)
            check("...with a message the operator can act on",
                  "Reload the page" in err.detail["message"], err.detail["message"][:60])
        await conn.execute("ROLLBACK TO SAVEPOINT s_fk")

        print("\nUNKNOWN SQLSTATES STILL 500 -- BUT ARE LOGGED, NOT SILENT")
        fake = asyncpg.PostgresError("something nobody predicted")
        err = st_router._db_failure(fake, "create_transaction", item="x")
        check("unmapped error is a 500", err.status_code == 500, "status=%s" % err.status_code)
        check("...named database_error", err.detail["error"] == "database_error")
        check("...and tells the operator it was recorded",
              "logged" in err.detail["message"])
    finally:
        await tx.rollback()
        await conn.close()

    print("\n%d passed, %d failed" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
