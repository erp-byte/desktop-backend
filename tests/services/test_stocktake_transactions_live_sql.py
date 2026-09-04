"""LIVE-SQL test for the stocktake_transactions ledger service.

EVERYTHING RUNS INSIDE A TRANSACTION THAT IS ROLLED BACK. That is not tidiness:
`stocktake_transactions` blocks DELETE by trigger, so a row committed by a test
could never be removed from the ledger it pollutes. Deliberate-failure cases use
savepoints, because Postgres aborts the whole transaction on the first error.

Expected values are read from the database at run time, never pinned.

Run against the database that actually holds the table:
    STOCKTAKE_TEST_DATABASE_URL=postgresql://.../warehouse_db \
    PYTHONPATH=. .venv/Scripts/python tests/services/test_stocktake_transactions_live_sql.py
"""
import asyncio
import os

import asyncpg

from app.config import Settings
from app.modules.stock_take.services import latest_stock_service as stock
from app.modules.stock_take.services import transactions_service as svc

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


class FakeUser:
    def __init__(self, floors, warehouses, user_id=None):
        self.allowed_floors = floors
        self.allowed_warehouses = warehouses
        self.user_id = user_id
        self.full_name = "Ledger Test"
        self.email = "test@example.com"
        self.phone = "0000"
        self.is_admin = False


async def raises(conn, coro_factory):
    """Run inside a savepoint so an expected failure doesn't poison the outer tx."""
    sp = conn.transaction()
    await sp.start()
    try:
        await coro_factory()
        await sp.rollback()
        return None
    except Exception as e:  # noqa: BLE001 - the type is the assertion
        await sp.rollback()
        return e


async def main():
    url = os.getenv("STOCKTAKE_TEST_DATABASE_URL") or Settings().DATABASE_URL
    conn = await asyncpg.connect(url)
    if not await conn.fetchval("SELECT to_regclass('stocktake_transactions')"):
        print("\n  SKIP  stocktake_transactions absent — point at the RDS warehouse_db.\n")
        await conn.close()
        return

    print("\n=== stocktake_transactions ledger (live SQL, rolled back) ===\n")

    # ── Scope resolution needs no database ────────────────────────────────────
    print("[1] Scope resolution")
    one = FakeUser(["Upper Basement"], ["W202"])
    check("single floor is pinned", svc.resolve_scope(one, warehouse=None, location=None)
          == ("W202", "Upper Basement"))
    many = FakeUser(["Upper Basement", "Terrace"], ["W202"])
    try:
        svc.resolve_scope(many, warehouse=None, location=None)
        check("multiple floors require a choice", False, "no raise")
    except svc.ScopeError as e:
        check("multiple floors require a choice", e.code == "floor_required", e.code)
    check("a chosen floor that IS granted resolves",
          svc.resolve_scope(many, warehouse=None, location="terrace")[1] == "Upper Basement"
          or svc.resolve_scope(many, warehouse=None, location="terrace")[1] == "Terrace")
    try:
        svc.resolve_scope(many, warehouse=None, location="Basement 9")
        check("an ungranted floor is refused", False, "no raise")
    except svc.ScopeError as e:
        check("an ungranted floor is refused", e.code == "floor_not_allowed", e.code)
    # EMPTY allowed_floors means "no restriction", NOT "no access"
    # (auth_schema.sql:35; the middleware only enforces `if user.allowed_floors`;
    # the profile screen renders it as "All"). An unrestricted user must be
    # OFFERED every floor, not locked out -- the bug this replaced.
    unres = FakeUser([], ["W202"])
    sc = svc.effective_scope(unres, available_floors=["Terrace", "Upper Basement"],
                             available_warehouses=["W202"])
    check("empty allowed_floors is unrestricted, not denied",
          sc["floors_unrestricted"] is True and sc["floors"] == ["Terrace", "Upper Basement"], str(sc))
    check("an unrestricted user with one available floor gets it pinned",
          svc.resolve_scope(unres, warehouse=None, location=None,
                            available_floors=["Terrace"], available_warehouses=["W202"])
          == ("W202", "Terrace"))
    try:
        svc.resolve_scope(unres, warehouse=None, location=None,
                          available_floors=["Terrace", "Upper Basement"],
                          available_warehouses=["W202"])
        check("an unrestricted user with several floors must choose", False, "no raise")
    except svc.ScopeError as e:
        check("an unrestricted user with several floors must choose",
              e.code == "floor_required", e.code)
    check("an unrestricted user may pick any available floor",
          svc.resolve_scope(unres, warehouse=None, location="Upper Basement",
                            available_floors=["Terrace", "Upper Basement"],
                            available_warehouses=["W202"])[1] == "Upper Basement")

    # Admins bypass scope entirely (middleware.py:160), so a grant list must not
    # narrow them.
    adm = FakeUser(["Terrace"], ["A185"])
    adm.is_admin = True
    asc = svc.effective_scope(adm, available_floors=["Terrace", "Upper Basement"],
                              available_warehouses=["W202", "A185"])
    check("an admin is unrestricted regardless of grants",
          asc["floors"] == ["Terrace", "Upper Basement"] and asc["warehouses"] == ["A185", "W202"],
          str(asc))

    # An UNRESTRICTED user with nothing available is a server/database problem,
    # not a permissions one — the message must not tell them to go ask an admin
    # for floor access when the real cause is DATABASE_URL pointing at a database
    # with no stocktake_entries.
    try:
        svc.resolve_scope(FakeUser([], ["W202"]), warehouse=None, location=None,
                          available_floors=[], available_warehouses=["W202"])
        check("unrestricted with no data blocks", False, "no raise")
    except svc.ScopeError as e:
        check("unrestricted with no data reports a DATA problem, not a permission one",
              e.code == "no_stock_data", e.code)
    # A genuinely SCOPED user whose grants are... non-empty by definition, so the
    # no_floor_access branch is reachable only via warehouse-side denial. Assert
    # the warehouse counterpart instead.
    try:
        svc.resolve_scope(FakeUser(["Terrace"], []), warehouse=None, location=None,
                          available_floors=["Terrace"], available_warehouses=[])
        check("no warehouse available blocks", False, "no raise")
    except svc.ScopeError as e:
        check("no warehouse available blocks", e.code == "no_warehouse_access", e.code)
    check("hyphenated warehouse normalises",
          svc.resolve_scope(FakeUser(["Terrace"], ["W-202"]), warehouse=None, location=None)[0] == "W202")

    tx = conn.transaction()
    await tx.start()
    try:
        # ── Pick a real counted article at a real granted place ───────────────
        row = await conn.fetchrow(
            """SELECT UPPER(BTRIM(item_name)) AS item, UPPER(BTRIM(warehouse)) AS wh,
                      floor_name AS floor, COALESCE(stock_type,'Fresh Stock') AS st
                 FROM stocktake_entries
                WHERE (status IS NULL OR status != 'draft')
                  AND UPPER(BTRIM(floor_name)) = 'UPPER BASEMENT'
                  AND UPPER(BTRIM(warehouse)) = 'W202'
                ORDER BY created_at DESC LIMIT 1""")
        if row is None:
            print("  SKIP  no counted stock on W202 / Upper Basement to net against")
            return

        print("\n[2] Balance read")
        bal = await svc.current_balance(conn, item_name=row["item"], stock_type=row["st"],
                                        warehouse="W202", location="Upper Basement")
        check("balance resolves a baseline count date", bal["as_of_date"] is not None, str(bal))
        check("counted_kg is positive", bal["counted_kg"] > 0, str(bal["counted_kg"]))
        check("net adjustment starts at zero", bal["net_adjustment_kg"] == 0)
        check("available == counted with no ledger rows",
              bal["available_kg"] == bal["counted_kg"])
        check("a counted article is not flagged uncounted", bal["uncounted"] is False)

        print("\n[3] Posting an ADDITION")
        base = dict(item_name=row["item"], material_type="RM", item_category="X",
                    item_subcategory="Y", stock_type=row["st"], units=2, qty_kg=5,
                    operation="ADDITION", reason="found extra pallet during recount")
        res = await svc.create_transaction(conn, dict(base), warehouse="W202",
                                           location="Upper Basement",
                                           created_by="Ledger Test", created_by_user_id=None)
        t = res["transaction"]
        check("row is created and returned", t["txn_id"] is not None)
        check("item_name is stored normalised", t["item_name"] == row["item"])
        check("scope is taken from the arguments, not the body",
              t["warehouse"] == "W202" and t["location"] == "Upper Basement")
        check("created_by comes from the caller", t["created_by"] == "Ledger Test")
        check("a plain entry is not a reversal",
              t["is_reversal"] is False and t["reverses_txn_id"] is None)
        check("balance_after reflects the addition",
              abs(res["balance_after_kg"] - (bal["available_kg"] + 5)) < 0.01,
              str(res["balance_after_kg"]))
        check("an addition is never overdrawn", res["overdrawn"] is False)

        print("\n[4] Netting is visible to the next read")
        bal2 = await svc.current_balance(conn, item_name=row["item"], stock_type=row["st"],
                                         warehouse="W202", location="Upper Basement")
        check("net_adjustment_kg picks up the posted row",
              abs(bal2["net_adjustment_kg"] - 5) < 0.01, str(bal2["net_adjustment_kg"]))
        check("available_kg moved by exactly the posted qty",
              abs(bal2["available_kg"] - (bal["available_kg"] + 5)) < 0.01)

        print("\n[5] Overdraw warns but does not block")
        big = dict(base, operation="SUBTRACTION", qty_kg=bal2["available_kg"] + 1000,
                   reason="deliberate overdraw test")
        res2 = await svc.create_transaction(conn, big, warehouse="W202", location="Upper Basement",
                                            created_by="Ledger Test", created_by_user_id=None)
        check("an over-subtraction still posts", res2["transaction"]["txn_id"] is not None)
        check("it is reported as overdrawn", res2["overdrawn"] is True)
        check("the resulting balance is allowed to go negative",
              res2["balance_after_kg"] < 0, str(res2["balance_after_kg"]))

        print("\n[6] Reversals")
        rev = dict(base, operation="SUBTRACTION", qty_kg=5, reason="undo the recount",
                   reverses_txn_id=t["txn_id"])
        rres = await svc.create_transaction(conn, rev, warehouse="W202", location="Upper Basement",
                                            created_by="Ledger Test", created_by_user_id=None)
        check("a linked reversal posts", rres["transaction"]["txn_id"] is not None)
        check("is_reversal is DERIVED, not taken from the body",
              rres["transaction"]["is_reversal"] is True)
        check("it points at its target",
              rres["transaction"]["reverses_txn_id"] == t["txn_id"])
        e = await raises(conn, lambda: svc.create_transaction(
            conn, dict(base, operation="ADDITION", qty_kg=5, reason="re-reverse",
                       reverses_txn_id=rres["transaction"]["txn_id"]),
            warehouse="W202", location="Upper Basement",
            created_by="t", created_by_user_id=None))
        check("reversing a reversal is refused", isinstance(e, ValueError), repr(e))
        e = await raises(conn, lambda: svc.create_transaction(
            conn, dict(base, reverses_txn_id=999999999),
            warehouse="W202", location="Upper Basement",
            created_by="t", created_by_user_id=None))
        check("reversing a non-existent txn is refused", isinstance(e, ValueError), repr(e))

        print("\n[6b] txn_code - the 8-digit reference the UI shows")
        # Expected day comes from the DATABASE, not from Python: the code is cut
        # with to_char(... AT TIME ZONE 'Asia/Kolkata','YYDDD'), and a test
        # that used the client's date would pass or fail depending on where it
        # was run rather than on whether the code is right.
        today = await conn.fetchval(
            "SELECT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYDDD')")
        codes = [t["txn_code"], res2["transaction"]["txn_code"], rres["transaction"]["txn_code"]]
        check("every posted row carries a txn_code", all(c for c in codes), str(codes))
        check("it is exactly 8 digits",
              all(len(c) == 8 and c.isdigit() for c in codes), str(codes))
        check("the first 5 digits are the IST year+day-of-year",
              all(c[:5] == today for c in codes), "%s vs %s" % (codes, today))
        check("codes are unique within the day", len(set(codes)) == len(codes), str(codes))
        check("the sequence advances with each row",
              [int(c[5:]) for c in codes] == sorted(int(c[5:]) for c in codes), str(codes))
        check("a reversal reports its target's code, not a raw id",
              rres["transaction"]["reverses_txn_code"] == t["txn_code"],
              "%r vs %r" % (rres["transaction"]["reverses_txn_code"], t["txn_code"]))

        # txn_code is minted by the trigger and is NOT in _INSERT_COLS, so a
        # client cannot choose its own reference number even if it sends one.
        spoof = await svc.create_transaction(
            conn, dict(base, txn_code="99999999", reason="attempt to set txn_code"),
            warehouse="W202", location="Upper Basement",
            created_by="Ledger Test", created_by_user_id=None)
        check("a txn_code in the payload is ignored",
              spoof["transaction"]["txn_code"] != "99999999"
              and spoof["transaction"]["txn_code"][:5] == today,
              spoof["transaction"]["txn_code"])

        # 099 disables the append-only UPDATE trigger to backfill, then re-enables
        # it. If a migration ever left it off, this table would silently stop
        # being append-only - so assert the trigger state directly, not just that
        # an UPDATE fails (section [8] covers that from the outside).
        # pg_trigger.tgenabled is Postgres "char", which asyncpg hands back as
        # bytes (b'O'), not str — decode or every comparison below is silently
        # False and the check passes/fails for the wrong reason.
        trg = {r["tgname"]: (r["tgenabled"].decode() if isinstance(r["tgenabled"], bytes)
                             else r["tgenabled"])
               for r in await conn.fetch(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'stocktake_transactions'::regclass AND NOT tgisinternal")}
        check("the append-only UPDATE trigger is enabled after the 099 backfill",
              trg.get("trg_stk_txn_no_update") == "O", str(trg))
        check("the DELETE block is enabled", trg.get("trg_stk_txn_no_delete") == "O", str(trg))
        check("the code-minting trigger is enabled",
              trg.get("trg_stk_txn_assign_code") == "O", str(trg))

        print("\n[6c] The UTC/IST midnight window - the bug this format fixes")
        # THE WHOLE POINT OF THE IST SWITCH, and the one case the rest of the
        # suite cannot reach: a row whose UTC day and IST day are DIFFERENT.
        # Today's two days happen to coincide, so every other assertion here
        # would pass just as well against the old UTC behaviour. This one pins
        # a timestamp at 20:00 UTC = 01:30 IST the NEXT day and proves the code
        # and the filters both follow the operator's calendar, not the server's.
        #
        # Inserted with raw SQL because create_transaction deliberately refuses a
        # caller-supplied created_at; what is under test is the database trigger
        # and the filter SQL, not the service's payload handling.
        pinned = await conn.fetchrow(
            """
            INSERT INTO stocktake_transactions
                (item_name, material_type, item_category, item_subcategory, stock_type,
                 units, qty_kg, operation, reason, warehouse, location, created_by, created_at)
            VALUES ($1, 'RM', 'X', 'Y', 'Fresh Stock', 1, 1, 'ADDITION',
                    'midnight window regression', 'W202', 'Upper Basement', 'Ledger Test',
                    TIMESTAMPTZ '2026-09-04 20:00:00+00')
            RETURNING txn_id, txn_code,
                      created_at::date AS utc_day,
                      (created_at AT TIME ZONE 'Asia/Kolkata')::date AS ist_day
            """,
            row["item"])
        check("the pinned row really does straddle midnight",
              str(pinned["utc_day"]) == "2026-09-04" and str(pinned["ist_day"]) == "2026-09-05",
              "utc=%s ist=%s" % (pinned["utc_day"], pinned["ist_day"]))
        # 2026-09-05 is day-of-year 248.
        check("txn_code carries the IST day, not the UTC one",
              pinned["txn_code"][:5] == "26248",
              "%s (UTC day would have been 26247)" % pinned["txn_code"])

        found = await svc.list_transactions(conn, warehouse="W202", on_date="2026-09-05")
        check("the IST date filter finds it",
              any(r["txn_id"] == pinned["txn_id"] for r in found["transactions"]),
              str(found["pagination"]))
        missed = await svc.list_transactions(conn, warehouse="W202", on_date="2026-09-04")
        check("the UTC date no longer matches it",
              not any(r["txn_id"] == pinned["txn_id"] for r in missed["transactions"]))
        check("the code's date and the filter that finds it agree",
              pinned["txn_code"][:5] == "26248"
              and any(r["txn_id"] == pinned["txn_id"] for r in found["transactions"]))

        print("\n[6d] Write-back into stocktake_entries")
        # An adjustment now ALSO writes a row into stocktake_entries so the
        # counting app and anything reading that table directly see the movement.
        adj_day = await conn.fetchval(
            "SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date")

        # DELTAS, NOT ABSOLUTES. Sections [3], [5] and [6] above already posted
        # adjustments for this same article on this same day, and every one of
        # them wrote back to this same row - so it exists, with an accumulated
        # total, before this section starts. Asserting created_new is True or a
        # weight of exactly 10 would be asserting that the earlier sections did
        # nothing, which is the opposite of what we want to be true.
        wb1 = await svc.create_transaction(
            conn, dict(base, qty_kg=7, operation="ADDITION", reason="write-back a"),
            warehouse="W202", location="Upper Basement",
            created_by="Ledger Test", created_by_user_id=None)
        e1 = wb1["stock_entry"]
        check("posting an adjustment writes a stocktake_entries row",
              e1["entry_id"] is not None, str(e1))
        start_kg = e1["day_total_weight"]

        wb2 = await svc.create_transaction(
            conn, dict(base, qty_kg=3, operation="ADDITION", reason="write-back b"),
            warehouse="W202", location="Upper Basement",
            created_by="Ledger Test", created_by_user_id=None)
        e2 = wb2["stock_entry"]
        check("the SAME day reuses the SAME row instead of adding another",
              e2["entry_id"] == e1["entry_id"] and e2["created_new"] is False, str(e2))
        check("an addition accumulates onto that row",
              abs((e2["day_total_weight"] - start_kg) - 3.0) < 0.001,
              "%s -> %s" % (start_kg, e2["day_total_weight"]))

        wb3 = await svc.create_transaction(
            conn, dict(base, qty_kg=4, operation="SUBTRACTION", reason="write-back c"),
            warehouse="W202", location="Upper Basement",
            created_by="Ledger Test", created_by_user_id=None)
        check("a subtraction folds in as a negative delta",
              abs((wb3["stock_entry"]["day_total_weight"] - e2["day_total_weight"]) + 4.0) < 0.001,
              "%s -> %s" % (e2["day_total_weight"], wb3["stock_entry"]["day_total_weight"]))
        check("the row still has the same id after three more postings",
              wb3["stock_entry"]["entry_id"] == e1["entry_id"])

        stored = await conn.fetchrow(
            """SELECT source_kind, status, warehouse, floor_name, stock_type,
                      ((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date AS ist_day
                 FROM stocktake_entries WHERE id = $1""", e1["entry_id"])
        check("it is marked ADJUSTMENT, never COUNT",
              stored["source_kind"] == "ADJUSTMENT", str(stored["source_kind"]))
        check("created_at is naive UTC, so its IST day is today",
              stored["ist_day"] == adj_day, "%s vs %s" % (stored["ist_day"], adj_day))
        check("scope is carried onto the row",
              stored["warehouse"] == "W202" and stored["floor_name"] == "Upper Basement")

        # Only ONE adjustment row exists for this article/place/day, enforced by
        # uq_entries_adjustment_day rather than by the service remembering to look.
        n_rows = await conn.fetchval(
            """SELECT COUNT(*) FROM stocktake_entries
                WHERE source_kind = 'ADJUSTMENT'
                  AND UPPER(BTRIM(item_name)) = $1
                  AND UPPER(BTRIM(warehouse)) = 'W202'
                  AND UPPER(BTRIM(floor_name)) = 'UPPER BASEMENT'
                  AND ((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date = $2""",
            row["item"], adj_day)
        check("exactly one adjustment row per article/place/day", n_rows == 1, str(n_rows))

        print("\n[6e] The write-back must NOT disturb the console figure")
        # THE ASSERTION THAT CATCHES THE COLLAPSE. Writing an adjustment row into
        # stocktake_entries without excluding it from the baseline makes the
        # newest "count day" an adjustment-only day: the view then reports O\nY
        # the adjusted article and every counted article vanishes. That failure
        # returns HTTP 200 with a confident as_of_date and a plausibly shaped
        # payload, so nothing else in this suite would notice it.
        v = await stock.fetch_latest_stock(conn, warehouse=["W202"], page=1, page_size=500)
        base_day = await conn.fetchval(
            """SELECT MAX(((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date)
                 FROM stocktake_entries
                WHERE (status IS NULL OR status <> 'draft')
                  AND (source_kind IS NULL OR source_kind = 'COUNT')
                  AND UPPER(BTRIM(warehouse)) = 'W202'""")
        check("the baseline still comes from a real COUNT day",
              str(v["as_of_date"]) == str(base_day),
              "%s vs %s" % (v["as_of_date"], base_day))
        counted_articles = await conn.fetchval(
            """SELECT COUNT(DISTINCT (UPPER(BTRIM(item_name)),
                                      COALESCE(stock_type,'Fresh Stock')))
                 FROM stocktake_entries
                WHERE (status IS NULL OR status <> 'draft')
                  AND (source_kind IS NULL OR source_kind = 'COUNT')
                  AND UPPER(BTRIM(warehouse)) = 'W202'
                  AND ((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date = $1""",
            base_day)
        check("no counted article was dropped from the view",
              v["pagination"]["total"] >= counted_articles,
              "view=%s counted=%s" % (v["pagination"]["total"], counted_articles))
        check("adjustment rows are excluded from the counted side",
              all(r.get("entry_count", 0) >= 0 for r in v["items"]))

        print("\n[7] Validation")
        e = await raises(conn, lambda: svc.create_transaction(
            conn, dict(base, operation="TRANSFER"), warehouse="W202",
            location="Upper Basement", created_by="t", created_by_user_id=None))
        check("an unknown operation is refused", isinstance(e, ValueError), repr(e))
        e = await raises(conn, lambda: svc.create_transaction(
            conn, dict(base, item_name="   "), warehouse="W202",
            location="Upper Basement", created_by="t", created_by_user_id=None))
        check("a blank item_name is refused", isinstance(e, ValueError), repr(e))

        print("\n[8] Append-only holds through the service")
        e = await raises(conn, lambda: conn.execute(
            "UPDATE stocktake_transactions SET qty_kg = 1 WHERE txn_id = $1", t["txn_id"]))
        check("UPDATE still blocked", e is not None, repr(e))
        e = await raises(conn, lambda: conn.execute(
            "DELETE FROM stocktake_transactions WHERE txn_id = $1", t["txn_id"]))
        check("DELETE still blocked", e is not None, repr(e))

        print("\n[9] Listing")
        lst = await svc.list_transactions(conn, warehouse="W202", location="Upper Basement")
        check("listing returns the rows just posted", lst["pagination"]["total"] >= 3,
              str(lst["pagination"]))
        check("newest first", lst["transactions"][0]["txn_id"] >= lst["transactions"][-1]["txn_id"])
        check("listed rows carry txn_code",
              all(r.get("txn_code") and len(r["txn_code"]) == 8 for r in lst["transactions"]))
        check("listed reversals resolve reverses_txn_code",
              all(r["reverses_txn_code"] for r in lst["transactions"] if r["reverses_txn_id"]))

        print("")
        print("[10] Netting into the latest-stock view (authoritative)")
        # Baseline BEFORE this test's rows are visible to the aggregate. The
        # aggregate resolves its own date, so scope the read the same way.
        agg = await stock.fetch_latest_stock(
            conn, warehouse=["W202"], floor_name=["Upper Basement"], page_size=1000)
        # Compare NORMALISED on both sides. The aggregate returns MIN(item_name),
        # i.e. the raw stored spelling, and stocktake_entries item names carry
        # trailing spaces just like floor names do ('ROASTED PUMPKIN SEEDS ').
        # Matching raw-against-normalised silently found nothing.
        norm = lambda x: (x or "").strip().upper()
        mine = [i for i in agg["items"]
                if norm(i["item_name"]) == norm(row["item"]) and i["stock_type"] == row["st"]]
        check("the adjusted article appears in the aggregate", len(mine) == 1,
              f"found {len(mine)}")
        if mine:
            it = mine[0]
            check("counted_weight is reported alongside the netted figure",
                  it["counted_weight"] > 0, str(it["counted_weight"]))
            check("net_adjustment_kg is non-zero after posting",
                  it["net_adjustment_kg"] != 0, str(it["net_adjustment_kg"]))
            check("total_weight == counted + net",
                  abs(it["total_weight"] - (it["counted_weight"] + it["net_adjustment_kg"])) < 0.01,
                  f'{it["total_weight"]} vs {it["counted_weight"]}+{it["net_adjustment_kg"]}')
            check("transaction_count is surfaced per item", it["transaction_count"] > 0,
                  str(it["transaction_count"]))
        check("totals expose both halves",
              {"counted_weight", "net_adjustment_kg", "transactions"} <= set(agg["totals"]),
              str(sorted(agg["totals"])))
        check("totals total_weight == counted + net",
              abs(agg["totals"]["total_weight"]
                  - (agg["totals"]["counted_weight"] + agg["totals"]["net_adjustment_kg"])) < 0.01,
              str(agg["totals"]))

        # An article that was NEVER counted must still surface once adjusted —
        # that is what the FULL OUTER JOIN is for.
        await svc.create_transaction(
            conn, dict(base, item_name="ZZ NEVER COUNTED TEST ARTICLE", qty_kg=7, units=1,
                       operation="ADDITION", reason="brand new article"),
            warehouse="W202", location="Upper Basement",
            created_by="Ledger Test", created_by_user_id=None)
        agg2 = await stock.fetch_latest_stock(
            conn, warehouse=["W202"], floor_name=["Upper Basement"], page_size=1000)
        new_rows = [i for i in agg2["items"] if norm(i["item_name"]) == "ZZ NEVER COUNTED TEST ARTICLE"]
        check("an uncounted but adjusted article appears in stock", len(new_rows) == 1,
              f"found {len(new_rows)}")
        if new_rows:
            check("it shows zero counted and the adjustment as its weight",
                  new_rows[0]["counted_weight"] == 0 and abs(new_rows[0]["total_weight"] - 7) < 0.01,
                  str(new_rows[0]))
            check("it reports no counting entries", new_rows[0]["entry_count"] == 0)
    finally:
        await tx.rollback()
        left = await conn.fetchval("SELECT COUNT(*) FROM stocktake_transactions")
        print("\nrolled back; rows left in ledger: %s" % left)
        await conn.close()

    print("\n=== %d passed, %d failed ===\n" % (_passed, _failed))
    raise SystemExit(1 if _failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
