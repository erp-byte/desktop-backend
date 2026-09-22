"""LIVE-SQL test: floor stock agrees with the Stock Take screen, place by place.

floor_stock_service.fetch_floor_stock reads everything on one warehouse + floor
for the job card's material tab. Its figures must be the ones the Stock Take
screen shows for that same place, or an operator sees two different numbers for
the same shelf. So for EVERY place in the live table — every warehouse/floor that
holds counts, and every place that holds only ledger rows — it compares, per
article and stock type, against latest_stock_service.fetch_latest_stock filtered
to that one place:

    available_kg        == total_weight
    available_quantity  == total_quantity
    last_counted_date   == last_counted_date
    entry_count         == entry_count
    and the same set of (article, stock type) rows on both sides.

Expected values are read from the database at run time, never hardcoded: the
table grows while people count. Pure reads, in a READ ONLY session — nothing is
written.

`new_stock_entries` lives in the AWS RDS `warehouse_db`, not in the Supabase
database `.env` can point at, so run it against RDS:

    STOCKTAKE_TEST_DATABASE_URL=postgresql://.../warehouse_db \\
    PYTHONPATH=. .venv/Scripts/python tests/services/test_stock_take_floor_stock_live_sql.py

Against a database without the table it reports SKIP rather than failing.
"""
import asyncio
import os
import sys

import asyncpg

from app.config import Settings
from app.modules.stock_take.services import floor_stock_service as fs
from app.modules.stock_take.services import latest_stock_service as ls

_TOL = 1e-6


def _key(name, stock_type):
    return ((name or "").strip().upper(), stock_type or "Fresh Stock")


async def _latest_for_place(conn, wh, fl):
    """Every row fetch_latest_stock returns for one place, across all pages."""
    rows, page = [], 1
    while True:
        out = await ls.fetch_latest_stock(conn, warehouse=[wh], floor_name=[fl],
                                          page=page, page_size=1000)
        items = out.get("items") or []
        rows.extend(items)
        if len(items) < 1000:
            return rows
        page += 1


async def main() -> int:
    url = os.getenv("STOCKTAKE_TEST_DATABASE_URL") or Settings().DATABASE_URL
    conn = await asyncpg.connect(url)
    passed = failed = 0
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        if not await conn.fetchval("SELECT to_regclass('public.new_stock_entries') IS NOT NULL"):
            print("SKIP: this database has no new_stock_entries "
                  "(set STOCKTAKE_TEST_DATABASE_URL to the RDS warehouse_db)")
            return 0

        places = {(wh, fl) for wh, fls in (await ls.fetch_places(conn)).items() for fl in fls}
        # Places holding adjustments but no counts must agree too.
        for r in await conn.fetch(
            "SELECT DISTINCT REPLACE(UPPER(BTRIM(warehouse)), '-', '') AS wh, BTRIM(location) AS fl "
            "  FROM stocktake_transactions "
            " WHERE warehouse IS NOT NULL AND BTRIM(COALESCE(location, '')) <> ''"
        ):
            places.add((r["wh"], r["fl"]))

        for wh, fl in sorted(places):
            mine = await fs.fetch_floor_stock(conn, warehouse=wh, floor=fl)
            theirs = await _latest_for_place(conn, wh, fl)
            a = {_key(i["item_name"], i["stock_type"]): i for i in mine["items"]}
            b = {_key(i["item_name"], i["stock_type"]): i for i in theirs}
            problems = []
            if set(a) != set(b):
                problems.append(f"rows only in floor-stock {sorted(set(a) - set(b))[:3]}, "
                                f"only in latest-stock {sorted(set(b) - set(a))[:3]}")
            for k in set(a) & set(b):
                x, y = a[k], b[k]
                if abs(x["available_kg"] - float(y["total_weight"])) > _TOL:
                    problems.append(f"{k}: kg {x['available_kg']} vs {y['total_weight']}")
                if abs(x["available_quantity"] - float(y["total_quantity"])) > _TOL:
                    problems.append(f"{k}: qty {x['available_quantity']} vs {y['total_quantity']}")
                if x["last_counted_date"] != y["last_counted_date"]:
                    problems.append(f"{k}: counted {x['last_counted_date']} vs {y['last_counted_date']}")
                if x["entry_count"] != int(y["entry_count"]):
                    problems.append(f"{k}: entries {x['entry_count']} vs {y['entry_count']}")
            label = f"{wh} / {fl}: {len(a)} rows"
            if problems:
                failed += 1
                print(f"  FAIL  {label}")
                for p in problems[:5]:
                    print(f"          {p}")
            else:
                passed += 1
                print(f"  PASS  {label}")

        # The job card spells the plant with a hyphen; that must be the same place.
        if places:
            wh, fl = sorted(places)[0]
            hyph = await fs.fetch_floor_stock(conn, warehouse=wh[:1] + "-" + wh[1:], floor=f"  {fl.lower()} ")
            plain = await fs.fetch_floor_stock(conn, warehouse=wh, floor=fl)
            if hyph["items"] == plain["items"]:
                passed += 1
                print("  PASS  hyphenated warehouse + padded lower-case floor reads the same place")
            else:
                failed += 1
                print("  FAIL  hyphenated warehouse / padded floor read a different result")
    finally:
        await conn.close()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
