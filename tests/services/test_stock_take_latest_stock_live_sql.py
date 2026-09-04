"""LIVE-SQL test for the stock-take latest-stock service.

`stocktake_entries` is owned by the separate Stock Take app and lives in the AWS
RDS `warehouse_db`. It is NOT in this app's production_schema.sql, and it does
not exist in the Supabase database `.env` can be pointed at -- so the statements
here cannot be validated by a FakeConn that only records SQL. This file executes
them against the real database.

If the configured database has no `stocktake_entries`, the run reports SKIP with
an explanation rather than failing: that is a configuration state, not a bug.
Point it at RDS to actually exercise the SQL:

    STOCKTAKE_TEST_DATABASE_URL=postgresql://.../warehouse_db \
    PYTHONPATH=. .venv/Scripts/python tests/services/test_stock_take_latest_stock_live_sql.py

Otherwise:

    PYTHONPATH=. .venv/Scripts/python tests/services/test_stock_take_latest_stock_live_sql.py

Expected values are read from the database at run time, never hardcoded: the
table grows while people are counting, and a pinned snapshot would turn "a floor
manager submitted a batch" into a wall of red. Pure reads -- nothing is written.
"""
import asyncio
import os

import asyncpg

from app.config import Settings
from app.modules.stock_take.services import latest_stock_service as svc

_passed = 0
_failed = 0


# The IST business day of a stocktake_entries row. Mirrors
# app/modules/stock_take/services/business_day.ENTRY_DAY -- the two-step form is
# required because created_at is a NAIVE column holding UTC.
ED = "((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date"


def check(label, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS  %s" % label)
    else:
        _failed += 1
        print("  FAIL  %s %s" % (label, extra))


def near(a, b):
    """Within a hair -- NUMERIC SUM vs Python float."""
    return abs(float(a) - float(b)) < 0.01


async def main():
    url = os.getenv("STOCKTAKE_TEST_DATABASE_URL") or Settings().DATABASE_URL
    conn = await asyncpg.connect(url)
    try:
        exists = await conn.fetchval("SELECT to_regclass('stocktake_entries')")
        if not exists:
            print(
                "\n  SKIP  stocktake_entries is not in the configured database.\n"
                "        This app's DATABASE_URL points somewhere without the Stock Take\n"
                "        tables (the Supabase config has none). Set STOCKTAKE_TEST_DATABASE_URL\n"
                "        to the RDS warehouse_db to exercise these statements.\n"
            )
            return

        non_draft = ("(status IS NULL OR status != 'draft')"
                     " AND (source_kind IS NULL OR source_kind = 'COUNT')")
        latest = await conn.fetchval(
            f"SELECT MAX(((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date) FROM stocktake_entries WHERE {non_draft}"
        )
        latest_s = latest.isoformat()
        # GROUND TRUTH IS REBUILT THE WAY THE SERVICE DEFINES THE FIGURE.
        #
        # Two corrections are baked in here, both learned the hard way:
        #
        # 1. It must include the LEDGER. Summing stocktake_entries alone was
        #    right only while stocktake_transactions was empty -- "netting is a
        #    no-op" was an accident of there being no data. When adjustments
        #    landed it reported failures that were just the netting working, and
        #    when the baseline later rolled past them it went green again for the
        #    wrong reason.
        #
        # 2. It must carry each article/place forward from ITS OWN latest count,
        #    not from one global day. And duplicates must be SUMMED BEFORE the
        #    latest day is picked: a naive DISTINCT ON over raw rows finds the
        #    right article/place combinations but drops roughly a third of all
        #    stock, because 1168 groups hold more than one count row for the same
        #    article/place/day.
        #
        # The ledger window is PER PLACE for the same reason the service uses one
        # -- most stock sits on articles whose floors were counted on different
        # days, so a single date per article both drops and double-counts.
        day = await conn.fetchrow(
            f"""
            WITH scoped AS (SELECT * FROM stocktake_entries WHERE {non_draft}),
            place_day AS (
                SELECT UPPER(BTRIM(item_name)) AS k,
                       COALESCE(stock_type, 'Fresh Stock') AS st,
                       COALESCE(UPPER(BTRIM(warehouse)), '') AS wh,
                       COALESCE(UPPER(BTRIM(floor_name)), '') AS fl,
                       {ED} AS d,
                       SUM(total_quantity) AS q, SUM(total_weight) AS w,
                       COUNT(*)::int AS e
                  FROM scoped GROUP BY 1, 2, 3, 4, 5
            ),
            place_latest AS (
                SELECT * FROM (
                    SELECT p.*, ROW_NUMBER() OVER (PARTITION BY k, st, wh, fl
                                                       ORDER BY d DESC) AS rn
                      FROM place_day p) z
                 WHERE rn = 1
            ),
            counted AS (
                SELECT k, st, SUM(q) AS q, SUM(w) AS w, SUM(e)::int AS e
                  FROM place_latest GROUP BY 1, 2
            ),
            txn AS (
                SELECT UPPER(BTRIM(t.item_name)) AS k,
                       COALESCE(t.stock_type, 'Fresh Stock') AS st,
                       SUM(CASE WHEN t.operation = 'ADDITION' THEN t.qty_kg ELSE -t.qty_kg END) AS nk,
                       SUM(CASE WHEN t.operation = 'ADDITION' THEN t.units  ELSE -t.units  END) AS nu
                  FROM stocktake_transactions t
                  LEFT JOIN place_latest b
                         ON b.k  = UPPER(BTRIM(t.item_name))
                        AND b.st = COALESCE(t.stock_type, 'Fresh Stock')
                        AND b.wh = COALESCE(UPPER(BTRIM(t.warehouse)), '')
                        AND b.fl = COALESCE(UPPER(BTRIM(t.location)), '')
                 WHERE (b.d IS NULL
                        OR (t.created_at AT TIME ZONE 'Asia/Kolkata')::date >= b.d)
                 GROUP BY 1, 2
            )
            SELECT COUNT(*)::int                                                   AS items,
                   COALESCE(SUM(COALESCE(c.e, 0)), 0)::int                         AS entries,
                   COALESCE(SUM(COALESCE(c.q, 0) + COALESCE(t.nu, 0)), 0)::float8  AS qty,
                   COALESCE(SUM(COALESCE(c.w, 0) + COALESCE(t.nk, 0)), 0)::float8  AS wt
              FROM counted c FULL OUTER JOIN txn t ON c.k = t.k AND c.st = t.st
            """
        )
        print("\n=== stock-take latest-stock (live SQL) ===\n")
        print("  (live: %s -- %d rows, %d items, %.2f kg)\n"
              % (latest_s, day["entries"], day["items"], day["wt"]))

        # -- [1] Date resolution + totals ------------------------------------
        print("[1] Date resolution and totals")
        r = await svc.fetch_latest_stock(conn, page_size=5000)
        check("as_of_date equals the latest IST count day", r["as_of_date"] == latest_s,
              "got %r expected %r" % (r["as_of_date"], latest_s))
        check("as_of_date is a YYYY-MM-DD string, not a date object",
              isinstance(r["as_of_date"], str))
        check("entries total matches a direct COUNT", r["totals"]["entries"] == day["entries"],
              "got %s" % r["totals"]["entries"])
        check("weight total matches a direct SUM", near(r["totals"]["total_weight"], day["wt"]),
              "got %s expected %s" % (r["totals"]["total_weight"], day["wt"]))
        check("quantity total matches a direct SUM", near(r["totals"]["total_quantity"], day["qty"]),
              "got %s expected %s" % (r["totals"]["total_quantity"], day["qty"]))
        check("item total is the DISTINCT item+stock_type count",
              r["totals"]["items"] == day["items"], "got %s" % r["totals"]["items"])
        check("summing returned rows reproduces the weight total",
              near(sum(i["total_weight"] for i in r["items"]), day["wt"]))
        check("row count equals the item total", len(r["items"]) == day["items"],
              "%d rows vs %d items" % (len(r["items"]), day["items"]))

        # -- [2] Filters narrow BOTH the date and the totals -----------------
        print("\n[2] Filters")
        wh = await conn.fetchrow(
            f"""
            SELECT UPPER(TRIM(warehouse)) AS w,
                   MAX(((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date) AS d
            FROM stocktake_entries WHERE {non_draft}
            GROUP BY 1 ORDER BY MAX(created_at) ASC LIMIT 1
            """
        )
        rw = await svc.fetch_latest_stock(conn, warehouse=[wh["w"]], page_size=5000)
        check("a warehouse filter reports that warehouse's newest count day",
              rw["as_of_date"] == wh["d"].isoformat(),
              "got %r expected %r for %s" % (rw["as_of_date"], wh["d"].isoformat(), wh["w"]))
        # Scoped the same per-place way. NOT "that warehouse's single last count
        # day" any more: within one warehouse, different floors are counted on
        # different days and each carries forward from its own.
        wt = await conn.fetchval(
            f"""
            WITH scoped AS (
                SELECT * FROM stocktake_entries
                 WHERE {non_draft} AND UPPER(TRIM(warehouse)) = $1
            ),
            place_day AS (
                SELECT UPPER(BTRIM(item_name)) AS k,
                       COALESCE(stock_type, 'Fresh Stock') AS st,
                       COALESCE(UPPER(BTRIM(warehouse)), '') AS wh,
                       COALESCE(UPPER(BTRIM(floor_name)), '') AS fl,
                       {ED} AS d, SUM(total_weight) AS w
                  FROM scoped GROUP BY 1, 2, 3, 4, 5
            ),
            place_latest AS (
                SELECT * FROM (
                    SELECT p.*, ROW_NUMBER() OVER (PARTITION BY k, st, wh, fl
                                                       ORDER BY d DESC) AS rn
                      FROM place_day p) z
                 WHERE rn = 1
            )
            SELECT COALESCE((SELECT SUM(w) FROM place_latest), 0)::float8
                 + COALESCE((SELECT SUM(CASE WHEN t.operation = 'ADDITION'
                                             THEN t.qty_kg ELSE -t.qty_kg END)
                               FROM stocktake_transactions t
                               LEFT JOIN place_latest b
                                      ON b.k  = UPPER(BTRIM(t.item_name))
                                     AND b.st = COALESCE(t.stock_type, 'Fresh Stock')
                                     AND b.wh = COALESCE(UPPER(BTRIM(t.warehouse)), '')
                                     AND b.fl = COALESCE(UPPER(BTRIM(t.location)), '')
                              WHERE UPPER(BTRIM(t.warehouse)) = $1
                                AND (b.d IS NULL
                                     OR (t.created_at AT TIME ZONE 'Asia/Kolkata')::date >= b.d)), 0)::float8
            """,
            wh["w"],
        )
        check("totals are scoped to the filtered warehouse",
              near(rw["totals"]["total_weight"], wt),
              "got %s expected %s" % (rw["totals"]["total_weight"], wt))
        check("applied filters are echoed back", rw["filters"].get("warehouse") == [wh["w"]])

        rt = await svc.fetch_latest_stock(conn, item_type=["PM"], page_size=1000)
        check("item_type filter is honoured",
              all((i["item_type"] or "").upper() == "PM" for i in rt["items"]),
              "types: %s" % {i["item_type"] for i in rt["items"]})

        rs = await svc.fetch_latest_stock(conn, search="__NO_SUCH_ITEM__", page_size=10)
        check("a search matching nothing yields no date", rs["as_of_date"] is None)

        # -- [3] asOf --------------------------------------------------------
        print("\n[3] asOf")
        prev = await conn.fetchval(
            f"""SELECT MAX(((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date) FROM stocktake_entries
                WHERE {non_draft} AND ((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date < $1::date""",
            latest,
        )
        prev = prev.isoformat() if prev else None
        if prev:
            ra = await svc.fetch_latest_stock(conn, as_of=prev, page_size=10)
            check("asOf picks the latest count on or before that day", ra["as_of_date"] == prev,
                  "got %r expected %r" % (ra["as_of_date"], prev))
            check("asOf is echoed in applied filters", ra["filters"].get("asOf") == prev)
        else:
            print("  SKIP  only one count date present -- asOf ordering not exercised")
        rold = await svc.fetch_latest_stock(conn, as_of="1990-01-01", page_size=10)
        check("an asOf before any data returns as_of_date None", rold["as_of_date"] is None)
        try:
            await svc.fetch_latest_stock(conn, as_of="not-a-date")
            check("a malformed asOf raises rather than silently widening", False, "no raise")
        except ValueError:
            check("a malformed asOf raises rather than silently widening", True)

        # -- [4] Empty result is an answer ------------------------------------
        print("\n[4] Empty result")
        rn = await svc.fetch_latest_stock(conn, warehouse=["__NO_SUCH_WAREHOUSE__"], page_size=10)
        check("no match returns as_of_date None", rn["as_of_date"] is None)
        check("no match returns an empty item list", rn["items"] == [])
        check("no match zeroes the totals",
              rn["totals"]["entries"] == 0 and rn["totals"]["total_weight"] == 0)

        # -- [5] Drafts -------------------------------------------------------
        print("\n[5] Drafts")
        all_status = await conn.fetchval(
            "SELECT MAX(((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date) FROM stocktake_entries"
        )
        all_status = all_status.isoformat() if all_status else None
        check("default view excludes drafts", r["as_of_date"] == latest_s)
        rd = await svc.fetch_latest_stock(conn, include_drafts=True, page_size=10)
        check("include_drafts widens to the all-status MAX", rd["as_of_date"] == all_status,
              "got %r expected %r" % (rd["as_of_date"], all_status))

        # -- [6] Sorting and pagination ---------------------------------------
        print("\n[6] Sorting and pagination")
        wts = [i["total_weight"] for i in r["items"]]
        check("defaults to heaviest first", all(wts[i - 1] >= w for i, w in enumerate(wts) if i))
        ra2 = await svc.fetch_latest_stock(conn, sort_order="asc", page_size=1000)
        awts = [i["total_weight"] for i in ra2["items"]]
        check("sort_order=asc reverses it", all(awts[i - 1] <= w for i, w in enumerate(awts) if i))
        rp = await svc.fetch_latest_stock(conn, sort_by="__proto__", page_size=5)
        check("an unknown sort key falls back to the default",
              rp["sort"]["sort_by"] == svc.DEFAULT_SORT, "got %s" % rp["sort"]["sort_by"])
        check("pagination total counts ITEMS not raw rows",
              rp["pagination"]["total"] == day["items"],
              "got %s items %s rows %s" % (rp["pagination"]["total"], day["items"], day["entries"]))
        check("page is capped at page_size", len(rp["items"]) == min(5, day["items"]))
        if day["items"] > 5:
            rp2 = await svc.fetch_latest_stock(conn, sort_by="__proto__", page=2, page_size=5)
            k1 = {(i["item_name"], i["stock_type"]) for i in rp["items"]}
            k2 = {(i["item_name"], i["stock_type"]) for i in rp2["items"]}
            check("consecutive pages do not repeat an item", not (k1 & k2), "overlap %s" % (k1 & k2))

        # -- [7] Injection ----------------------------------------------------
        print("\n[7] Robustness")
        ri = await svc.fetch_latest_stock(conn, search="'; DROP TABLE stocktake_entries;--", page_size=5)
        check("injection attempt is parameterised harmlessly", ri["items"] == [])
        check("table survived",
              await conn.fetchval("SELECT COUNT(*) FROM stocktake_entries") > 0)

        # -- [8] Filter options ------------------------------------------------
        print("\n[8] Filter options")
        opts = await svc.fetch_filter_options(conn)
        check("filter options expose every dimension",
              set(opts) == {"warehouses", "floors", "item_types", "stock_types"})
        check("warehouses are populated", len(opts["warehouses"]) > 0,
              "got %s" % opts["warehouses"])
    finally:
        await conn.close()

    print("\n=== %d passed, %d failed ===\n" % (_passed, _failed))
    raise SystemExit(1 if _failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
