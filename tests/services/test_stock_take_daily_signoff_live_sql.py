"""LIVE-SQL test for the one-day sign-off on a stock line.

THE RULE. A line reads as verified only when every row behind it is signed AND the
newest of those signatures was given on the day being viewed (IST). The next
morning it shows Verify again. Nothing is written when the day turns over; the
tick is read from the signatures, so there is no reset job to run or to get wrong.

THE TRAP. "Every row signed today" looks equivalent and is not. A line's older
rows keep their older signatures, because Verify only moves rows that need it. On
5 Nov 2026, 53 of the 128 lines signed that day had such rows, and that reading
would have taken those ticks away from people still working through the floor.

THE OTHER HALF. Verify skips rows already signed, so a line signed yesterday would
show a button that does nothing. It must re-sign the line today, and only its
newest row, so the older signatures on the line survive.

FIXTURES ARE TEMP TABLES inside a transaction that is rolled back, copied from
the real shapes on RDS, including uq_nse_adjustment_day because write_back_entry's
ON CONFLICT needs it. No real table is read or written.

DELIBERATELY NO FALLBACK to Settings().DATABASE_URL, which can point at production.

Run against a throwaway Postgres:
    STOCKTAKE_TEST_DATABASE_URL=postgresql://... \
    PYTHONPATH=. .venv/Scripts/python tests/services/test_stock_take_daily_signoff_live_sql.py
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import asyncpg

from app.modules.stock_take.services import latest_stock_service as stock
from app.modules.stock_take.services import transactions_service as svc

IST = timezone(timedelta(hours=5, minutes=30))
WH, FLOOR, OTHER_FLOOR, STOCK = "W202", "Store", "Terrace", "Fresh Stock"

MIDNIGHT = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
TODAY = MIDNIGHT.date().isoformat()
YESTERDAY = (MIDNIGHT - timedelta(days=1)).date().isoformat()
# Today in IST but still yesterday in UTC, and the reverse trap just before midnight.
EARLY_TODAY = MIDNIGHT + timedelta(minutes=30)
LATE_YESTERDAY = MIDNIGHT - timedelta(minutes=30)


def days_ago(n: int) -> datetime:
    return MIDNIGHT - timedelta(days=n) + timedelta(hours=12)


DDL = """
CREATE TEMP TABLE new_stock_entries (
    id bigserial PRIMARY KEY,
    item_name varchar NOT NULL,
    item_type varchar, item_category varchar, item_subcategory varchar,
    warehouse varchar, floor_name varchar,
    total_quantity numeric(10,2) DEFAULT 0,
    unit_uom numeric(10,3) DEFAULT 0.0,
    total_weight numeric(10,2) NOT NULL DEFAULT 0.0,
    entered_by varchar NOT NULL,
    entered_by_email varchar,
    authority varchar NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    stock_type varchar NOT NULL DEFAULT 'Fresh Stock',
    entry_id varchar,
    status varchar NOT NULL DEFAULT 'submitted',
    is_checked boolean NOT NULL DEFAULT false,
    verified boolean NOT NULL DEFAULT false,
    verified_by varchar,
    verified_at timestamptz,
    remark varchar, edits jsonb,
    source_kind varchar NOT NULL DEFAULT 'COUNT',
    movement_id bigint
);
CREATE UNIQUE INDEX uq_nse_adjustment_day ON new_stock_entries
    USING btree (((created_at AT TIME ZONE 'Asia/Kolkata')::date),
                 upper(btrim(item_name::text)),
                 upper(btrim(COALESCE(warehouse, '')::text)),
                 upper(btrim(COALESCE(floor_name, '')::text)),
                 stock_type)
    WHERE source_kind::text = 'ADJUSTMENT'::text;
CREATE TEMP TABLE stocktake_transactions (
    txn_id bigserial PRIMARY KEY,
    item_name text NOT NULL,
    sku_id integer,
    is_new_article boolean NOT NULL DEFAULT false,
    material_type text NOT NULL, item_category text NOT NULL, item_subcategory text NOT NULL,
    stock_type text NOT NULL DEFAULT 'Fresh Stock',
    units numeric(15,3) NOT NULL, qty_kg numeric(15,3) NOT NULL,
    operation text NOT NULL, reason text NOT NULL,
    warehouse text NOT NULL, location text NOT NULL,
    reverses_txn_id bigint,
    is_reversal boolean NOT NULL DEFAULT false,
    created_by text NOT NULL,
    created_by_user_id integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    txn_code text NOT NULL,
    verified boolean NOT NULL DEFAULT false,
    verified_by text,
    verified_at timestamptz
);
"""

_passed = 0
_failed = 0


def check(label, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print("  PASS  %s" % label)
    else:
        _failed += 1
        print("  FAIL  %s: got %r, want %r" % (label, got, want))


async def add(conn, item, created=None, signed=None, by=None, kind="COUNT",
              status="submitted", floor=FLOOR):
    """One entries row. `signed` is when it was signed, or None for unsigned."""
    return await conn.fetchval(
        """
        INSERT INTO new_stock_entries
            (item_name, warehouse, floor_name, total_quantity, total_weight, entered_by,
             authority, created_at, updated_at, stock_type, status, source_kind,
             verified, verified_by, verified_at)
        VALUES ($1, $2, $3, 1, 10, 'Counter', 'Test', $4, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        item, WH, floor, created or days_ago(30), STOCK, status, kind,
        signed is not None, by, signed)


async def line(conn, item, as_of=None):
    """The line as the adjust screen reads it: one warehouse, one floor."""
    r = await stock.fetch_latest_stock(
        conn, as_of=as_of, warehouse=[WH], floor_name=[FLOOR], search=item, page_size=50)
    return next((i for i in r["items"] if i["item_name"] == item), None)


async def ticked(conn, item, as_of=None):
    it = await line(conn, item, as_of)
    return it["verified"] if it else None


async def press_verify(conn, item, actor, verified=True):
    return await svc.verify_entries(
        conn, actor=actor, warehouse=WH, location=FLOOR, item_name=item,
        stock_type=STOCK, verified=verified)


async def stamp(conn, row_id):
    r = await conn.fetchrow(
        "SELECT verified, verified_by, verified_at FROM new_stock_entries WHERE id = $1", row_id)
    return r["verified"], r["verified_by"], r["verified_at"]


async def reading_the_tick(conn):
    print("\nREADING THE TICK")
    await add(conn, "SIGNED TODAY", signed=EARLY_TODAY + timedelta(hours=9), by="Amit")
    check("signed today keeps its tick", await ticked(conn, "SIGNED TODAY"), True)

    await add(conn, "SIGNED YESTERDAY", signed=days_ago(1), by="Amit")
    check("signed yesterday shows Verify", await ticked(conn, "SIGNED YESTERDAY"), False)

    await add(conn, "SIGNED LAST MONTH", signed=days_ago(31), by="Amit")
    check("signed long ago shows Verify", await ticked(conn, "SIGNED LAST MONTH"), False)

    await add(conn, "NEVER SIGNED")
    check("never signed shows Verify", await ticked(conn, "NEVER SIGNED"), False)

    print("\nTHE TRAP: A LINE SIGNED TODAY WITH OLDER ROWS")
    item = "OLDER ROWS PLUS TODAY"
    await add(conn, item, created=days_ago(40), signed=days_ago(20), by="Amit")
    await add(conn, item, created=days_ago(5), kind="ADJUSTMENT", signed=days_ago(5), by="Amit")
    await add(conn, item, created=EARLY_TODAY, kind="ADJUSTMENT",
              signed=EARLY_TODAY + timedelta(hours=9), by="Amit")
    check("keeps its tick", await ticked(conn, item), True)

    item = "ONE ROW STILL UNSIGNED"
    await add(conn, item, signed=EARLY_TODAY + timedelta(hours=9), by="Amit")
    await add(conn, item, created=EARLY_TODAY, kind="ADJUSTMENT")
    check("one unsigned row still blocks the tick", await ticked(conn, item), False)

    print("\nTHE DAY IS THE IST DAY")
    await add(conn, "JUST AFTER MIDNIGHT", signed=EARLY_TODAY, by="Amit")
    check("00:30 IST counts as today, though UTC is still on yesterday",
          await ticked(conn, "JUST AFTER MIDNIGHT"), True)
    await add(conn, "JUST BEFORE MIDNIGHT", signed=LATE_YESTERDAY, by="Amit")
    check("23:30 IST yesterday does not, though UTC is on the same date as 00:30",
          await ticked(conn, "JUST BEFORE MIDNIGHT"), False)

    print("\nWHO THE TICK NAMES")
    item = "TWO SIGNERS"
    await add(conn, item, created=days_ago(40), signed=days_ago(3), by="Zara")
    await add(conn, item, created=days_ago(1), kind="ADJUSTMENT",
              signed=EARLY_TODAY + timedelta(hours=9), by="Amit")
    it = await line(conn, item)
    check("the latest signer, not the alphabetically last", it["verified_by"], "Amit")

    print("\nA PAST DATE READS THAT DAY'S SIGN-OFF")
    item = "SIGNED ON THE DAY VIEWED"
    await add(conn, item, created=days_ago(5), signed=days_ago(1), by="Amit")
    check("ticked on the day it was signed", await ticked(conn, item, YESTERDAY), True)
    check("not ticked today", await ticked(conn, item), False)
    item = "SIGNED AFTER THE DAY VIEWED"
    await add(conn, item, created=days_ago(5), signed=EARLY_TODAY, by="Amit")
    check("a later sign-off does not show on an earlier day", await ticked(conn, item, YESTERDAY), False)

    print("\nA FLOOR SIGNS ONLY FOR ITSELF")
    item = "SIGNED ELSEWHERE"
    await add(conn, item, signed=days_ago(1), by="Amit")
    await add(conn, item, signed=EARLY_TODAY, by="Amit", floor=OTHER_FLOOR)
    check("another floor's sign-off today gives no tick here", await ticked(conn, item), False)


async def pressing_verify(conn):
    print("\nVERIFY ON A LINE SIGNED ON AN EARLIER DAY")
    item = "STALE LINE"
    old = await add(conn, item, created=days_ago(40), signed=days_ago(10), by="Old One")
    new = await add(conn, item, created=days_ago(5), kind="ADJUSTMENT", signed=days_ago(3), by="Old Two")
    r = await press_verify(conn, item, "Today Signer")
    check("reports one row signed, not 'already signed off'", r["verified_count"], 1)
    check("the tick comes back", await ticked(conn, item), True)
    check("the tick names today's signer", (await line(conn, item))["verified_by"], "Today Signer")
    check("the newest row carries the new signature", (await stamp(conn, new))[1], "Today Signer")
    check("the older row keeps its own signature",
          await stamp(conn, old), (True, "Old One", days_ago(10)))

    print("\nA SECOND CLICK THE SAME DAY REWRITES NOTHING")
    r = await press_verify(conn, item, "Someone Else")
    check("reports nothing to do", r["verified_count"], 0)
    check("still names the first signer today", (await line(conn, item))["verified_by"], "Today Signer")

    print("\nVERIFY ON A LINE ALREADY SIGNED TODAY")
    item = "SIGNED THIS MORNING"
    today_stamp = EARLY_TODAY + timedelta(hours=8)
    older = await add(conn, item, created=days_ago(40), signed=days_ago(9), by="Old One")
    newest = await add(conn, item, created=days_ago(1), kind="ADJUSTMENT", signed=today_stamp, by="Morning")
    r = await press_verify(conn, item, "Afternoon")
    check("reports nothing to do", r["verified_count"], 0)
    check("this morning's signature stands", await stamp(conn, newest), (True, "Morning", today_stamp))
    check("the older signature stands", await stamp(conn, older), (True, "Old One", days_ago(9)))

    print("\nVERIFY WHEN A NEW ROW IS UNSIGNED")
    item = "NEW ROW ON A STALE LINE"
    signed = await add(conn, item, created=days_ago(40), signed=days_ago(10), by="Old One")
    unsigned = await add(conn, item, created=days_ago(1), kind="ADJUSTMENT")
    r = await press_verify(conn, item, "Today Signer")
    check("signs only the unsigned row", r["verified_count"], 1)
    check("the tick comes back", await ticked(conn, item), True)
    check("the unsigned row is signed", (await stamp(conn, unsigned))[1], "Today Signer")
    check("the older signature stands", await stamp(conn, signed), (True, "Old One", days_ago(10)))

    print("\nVERIFY NEVER RESTS ON A DRAFT")
    item = "NEWEST ROW IS A DRAFT"
    await add(conn, item, created=days_ago(40), signed=days_ago(10), by="Old One")
    await add(conn, item, created=days_ago(1), status="draft")
    await press_verify(conn, item, "Today Signer")
    check("the tick comes back from a row the screen counts", await ticked(conn, item), True)

    print("\nVERIFY ON A LINE NEVER SIGNED")
    item = "NEVER SIGNED BEFORE"
    await add(conn, item, created=days_ago(40))
    await add(conn, item, created=days_ago(1), kind="ADJUSTMENT")
    r = await press_verify(conn, item, "Today Signer")
    check("signs every row, as before", r["verified_count"], 2)
    check("the tick appears", await ticked(conn, item), True)

    print("\nWITHDRAWING A SIGN-OFF")
    item = "WITHDRAWN"
    w = await add(conn, item, signed=days_ago(3), by="Old One")
    r = await press_verify(conn, item, "Today Signer", verified=False)
    check("clears the row", await stamp(conn, w), (False, None, None))
    check("is not re-signed", r["verified_count"], 1)
    check("shows Verify", await ticked(conn, item), False)

    print("\nSTOCK CHANGING STILL TAKES THE TICK OFF")
    item = "ADJUSTED AFTER SIGNING"
    await add(conn, item, created=days_ago(10), signed=days_ago(2), by="Old One")
    await press_verify(conn, item, "Today Signer")
    check("ticked before the adjustment", await ticked(conn, item), True)
    await svc.write_back_entry(
        conn, item_name=item, stock_type=STOCK, warehouse=WH, location=FLOOR,
        units_delta=1, kg_delta=10, actor="Poster",
        material_type="rm", item_category="dates", item_subcategory="fard")
    check("adjusting it shows Verify again", await ticked(conn, item), False)
    await press_verify(conn, item, "Today Signer")
    check("and it can be signed again", await ticked(conn, item), True)

    print("\nA POSTING'S OWN SIGN-OFF DOES NOT EXPIRE")
    item = "POSTING SIGNED EARLIER"
    await add(conn, item, created=days_ago(3), kind="ADJUSTMENT", signed=days_ago(3), by="Old One")
    txn = await conn.fetchval(
        """
        INSERT INTO stocktake_transactions
            (item_name, material_type, item_category, item_subcategory, stock_type, units, qty_kg,
             operation, reason, warehouse, location, created_by, created_at, txn_code,
             verified, verified_by, verified_at)
        VALUES ($1, 'rm', 'dates', 'fard', $2, 1, 10, 'ADDITION', 'test', $3, $4, 'Poster', $5,
                '260000001', TRUE, 'Old One', $5)
        RETURNING txn_id
        """, item, STOCK, WH, FLOOR, days_ago(3))
    r = await press_verify(conn, item, "Today Signer")
    check("no posting is re-signed", r["transactions_cascaded"], 0)
    row = await conn.fetchrow(
        "SELECT verified_by, verified_at FROM stocktake_transactions WHERE txn_id = $1", txn)
    check("the posting keeps its signer and time", (row["verified_by"], row["verified_at"]), ("Old One", days_ago(3)))

    print("\nTHE DAY'S BULK SIGN-OFF IS UNCHANGED")
    item = "BULK DAY ALREADY SIGNED"
    b = await add(conn, item, created=days_ago(2), kind="ADJUSTMENT", signed=days_ago(2), by="Old One")
    r = await svc.verify_entries(conn, actor="Today Signer", day=(MIDNIGHT - timedelta(days=2)).date(),
                                 warehouse=WH, location=FLOOR)
    check("skips rows already signed", b in {x["entry_id"] for x in r["rows"]}, False)
    check("rewrites no signature", await stamp(conn, b), (True, "Old One", days_ago(2)))


async def main() -> int:
    dsn = os.environ.get("STOCKTAKE_TEST_DATABASE_URL")
    if not dsn:
        raise SystemExit("Set STOCKTAKE_TEST_DATABASE_URL to a throwaway Postgres.")
    conn = await asyncpg.connect(dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(DDL)
        await reading_the_tick(conn)
        await pressing_verify(conn)
    finally:
        await tx.rollback()
        await conn.close()
    print("\n%d passed, %d failed" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
