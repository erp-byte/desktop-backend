"""Merge three A185 floors into others, in Stock Take only.

Asked for on 18 Nov 2026:
    FFS Packing Area       -> FG store
    Dmart Production Area  -> Dmart Packing Area   (shown as "Packing Area")
    Sorting Area           -> Dmart Packing Area

    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\apply_a185_floor_merge.py            # plan only, reads
    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\apply_a185_floor_merge.py --dry-run  # writes, checks, rolls back
    & .\\backend\\.venv\\Scripts\\python.exe .\\backend\\scripts\\apply_a185_floor_merge.py --apply    # writes, checks, commits

Back up first: scripts/backup_stock_take_tables.py.

HOW: ONE COUNT ROW PER ARTICLE AND PLACE, NOT A RENAME
The adjustment ledger (stocktake_transactions) refuses UPDATE and DELETE, so old
postings cannot be moved to another floor; and it only takes rows with units > 0
and kg > 0, while these floors hold lines in units only (Sorting Area: 88,681
units at 0 kg). So the merge is written the way a stock take writes stock: count
rows in new_stock_entries (source_kind 'COUNT', entry_id MRGmmdd, entered by
Admin), the same shape as the cold storage loads.

For every article (and stock type) with stock on a merged-away floor:
  * the floor it goes into gets a count of (its own stock + the merged floors' stock);
  * each merged-away floor gets a count that leaves it at 0.
Kg and units are both carried, negative values included, so every figure is the
same before and after except where it sits. Nothing is deleted and no old row is
edited; the history of the old floors stays as it was.

A count resets an article's place to that day, and postings made on the same day
still add on top of it (latest_stock_service nets ledger rows dated >= the count
day). So each row is worked out as "wanted - what today's rows already add", and
the script re-reads the stock after writing and refuses to commit unless:
  - every merged-away floor is at 0 kg and 0 units for every article it touched,
  - the floor it went into holds exactly the sum,
  - every other A185 line is unchanged, and each article's A185 total is unchanged.

RE-RUNNABLE. Run again later (e.g. after the screens stop offering the old floors)
and it moves whatever has been posted on them since; with nothing there it writes
nothing.

RUN IT AFTER THE DAY'S COUNTING. The rows it writes on FG store / Dmart Packing
Area are counts dated today, and the stock read ADDS every count of an article at
a place on one day. A count entered there LATER THE SAME DAY (a floor recount, a
draft submitted, a floor-app row synced in) is therefore added to the merge row
instead of replacing it, doubling that article until the place is counted on a
later day. Adjustments are not affected -- they net on top, as they should. A185
had no counts other than this merge in the 21 days to 18 Nov 2026 (its movements
are all adjustments), so this is a hazard to time around, not a daily event; the
script prints a reminder when it writes.

The RDS DSN is whichever DATABASE_URL line in backend/.env points at RDS. Both
tables are locked against new rows for the few seconds the merge takes, so a
posting cannot land between the read and the write.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import asyncpg

BACKEND = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(BACKEND), str(BACKEND / "scripts")]
from app.modules.stock_take.services.business_day import ENTRY_DAY, TXN_DAY  # noqa: E402
from app.modules.stock_take.services.latest_stock_service import fetch_stock_by_place  # noqa: E402
from backup_stock_take_tables import rds_dsn  # noqa: E402

WH = "A185"
#: merged-away floor -> the floor it goes into, both spelled as FLOORS_BY_WAREHOUSE does.
MERGE = {
    "FFS Packing Area": "FG store",
    "Dmart Production Area": "Dmart Packing Area",
    "Sorting Area": "Dmart Packing Area",
}
ACTOR = "Admin"
TINY = Decimal("0.0005")
Q3 = Decimal("0.001")
WH_KEY = "REPLACE(UPPER(BTRIM(COALESCE(warehouse, ''))), '-', '')"
SCRATCH = BACKEND / "scratch"


class _Rollback(Exception):
    pass


def d(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Q3, rounding=ROUND_HALF_UP)


def key(floor: str) -> str:
    return floor.strip().upper()


async def stock(conn) -> dict[tuple[str, str, str], dict]:
    """(floor key, article key, stock type) -> line, for all of A185."""
    rows, _ = await fetch_stock_by_place(conn, warehouse=[WH])
    out = {}
    for r in rows:
        out[(r["floor_key"], r["item_key"], r["stock_type"])] = {
            **r, "kg": d(r["total_weight"]), "units": d(r["total_quantity"])}
    return out


async def build(conn) -> tuple[list[dict], dict, str]:
    """The rows to write, the stock they were worked out from, and the IST day."""
    today = await conn.fetchval("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date")
    before = await stock(conn)
    places = sorted({key(f) for f in MERGE} | {key(t) for t in MERGE.values()})

    # What today's ledger rows already add at each place (they apply on top of a
    # count made today).
    today_txn = {}
    for r in await conn.fetch(f"""
            SELECT UPPER(BTRIM(location)) fl, UPPER(BTRIM(item_name)) it,
                   COALESCE(stock_type, 'Fresh Stock') st,
                   SUM(CASE WHEN operation = 'ADDITION' THEN qty_kg ELSE -qty_kg END) kg,
                   SUM(CASE WHEN operation = 'ADDITION' THEN units  ELSE -units  END) u
              FROM stocktake_transactions
             WHERE {WH_KEY} = $1 AND {TXN_DAY} = $2 AND UPPER(BTRIM(location)) = ANY($3)
             GROUP BY 1, 2, 3""", WH, today, places):
        today_txn[(r["fl"], r["it"], r["st"])] = (d(r["kg"]), d(r["u"]))
    # Places already counted today: a further count today is ADDED to theirs.
    counted_today = {(r["fl"], r["it"], r["st"]) for r in await conn.fetch(f"""
            SELECT DISTINCT UPPER(BTRIM(floor_name)) fl, UPPER(BTRIM(item_name)) it,
                   COALESCE(stock_type, 'Fresh Stock') st
              FROM new_stock_entries
             WHERE {WH_KEY} = $1 AND {ENTRY_DAY} = $2 AND UPPER(BTRIM(floor_name)) = ANY($3)
               AND (status IS NULL OR status != 'draft')
               AND (source_kind IS NULL OR source_kind = 'COUNT')""", WH, today, places)}

    def base(fl: str, it: str, st: str) -> tuple[Decimal, Decimal]:
        """What the place will show once a count row of 0 is added today."""
        if (fl, it, st) in counted_today:
            line = before.get((fl, it, st))
            return (line["kg"], line["units"]) if line else (Decimal(0), Decimal(0))
        return today_txn.get((fl, it, st), (Decimal(0), Decimal(0)))

    by_target = defaultdict(list)
    for src, tgt in MERGE.items():
        by_target[tgt].append(src)

    out = []
    for tgt, srcs in by_target.items():
        tk, sks = key(tgt), [key(s) for s in srcs]
        articles = sorted({(it, st) for (fl, it, st), line in before.items()
                           if fl in sks and (abs(line["kg"]) >= TINY or abs(line["units"]) >= TINY)})
        for it, st in articles:
            lines = {fl: before.get((fl, it, st)) for fl in [tk, *sks]}
            meta = next(line for line in [lines[tk], *(lines[s] for s in sks)] if line)
            want_kg = sum((ln["kg"] for ln in lines.values() if ln), Decimal(0))
            want_u = sum((ln["units"] for ln in lines.values() if ln), Decimal(0))
            plan = [(tgt, want_kg, want_u, "into")]
            plan += [(s, Decimal(0), Decimal(0), "out of") for s, sk in zip(srcs, sks)
                     if lines[sk] and (abs(lines[sk]["kg"]) >= TINY or abs(lines[sk]["units"]) >= TINY)]
            for floor, wkg, wu, way in plan:
                fk = key(floor)
                bkg, bu = base(fk, it, st)
                now_line = lines.get(fk)
                moved_from = [f"{s} {lines[key(s)]['kg']:,} kg / {lines[key(s)]['units']:,} u"
                              for s in srcs if lines[key(s)]]
                out.append({
                    "floor": floor, "item_key": it, "item_name": meta["item_name"],
                    "item_type": meta["item_type"], "item_category": meta["item_category"],
                    "item_subcategory": meta["item_subcategory"], "stock_type": st,
                    "kg": d(wkg - bkg), "units": d(wu - bu),
                    "was_kg": now_line["kg"] if now_line else Decimal(0),
                    "was_units": now_line["units"] if now_line else Decimal(0),
                    "want_kg": wkg, "want_units": wu, "group": tgt,
                    "remark": (f"Floor merge: {', '.join(srcs)} -> {tgt}. "
                               + (f"Merged in: {'; '.join(moved_from)}" if way == "into"
                                  else f"Moved to {tgt}"))[:255],
                })
    return out, before, today.strftime("%d-%m-%Y")


async def check(conn, before: dict, rows: list[dict]) -> list[str]:
    """Problems with the stock after the rows are written; [] when it is right."""
    after = await stock(conn)
    touched = {(key(r["floor"]), r["item_key"], r["stock_type"]): r for r in rows}
    problems = []
    for k in set(before) | set(after):
        b, a = before.get(k), after.get(k)
        akg, au = (a["kg"], a["units"]) if a else (Decimal(0), Decimal(0))
        if k in touched:
            want = touched[k]
            if abs(akg - want["want_kg"]) >= TINY or abs(au - want["want_units"]) >= TINY:
                problems.append(f"{k}: is {akg} kg / {au} u, wanted {want['want_kg']} kg / {want['want_units']} u")
        else:
            bkg, bu = (b["kg"], b["units"]) if b else (Decimal(0), Decimal(0))
            if abs(akg - bkg) >= TINY or abs(au - bu) >= TINY:
                problems.append(f"{k}: untouched line moved from {bkg} kg / {bu} u to {akg} kg / {au} u")
    tot_b, tot_a = defaultdict(lambda: [Decimal(0), Decimal(0)]), defaultdict(lambda: [Decimal(0), Decimal(0)])
    for src, tot in ((before, tot_b), (after, tot_a)):
        for (fl, it, st), line in src.items():
            tot[(it, st)][0] += line["kg"]
            tot[(it, st)][1] += line["units"]
    for k in set(tot_b) | set(tot_a):
        if any(abs(x - y) >= TINY for x, y in zip(tot_b[k], tot_a[k])):
            problems.append(f"A185 total for {k} moved: {tot_b[k]} -> {tot_a[k]}")
    for src in MERGE:
        for (fl, it, st), line in after.items():
            if fl == key(src) and (abs(line["kg"]) >= TINY or abs(line["units"]) >= TINY):
                problems.append(f"{src} still holds {it} ({st}): {line['kg']} kg / {line['units']} u")
    return problems


def report(rows: list[dict], before: dict) -> None:
    for tgt in dict.fromkeys(MERGE.values()):
        srcs = [s for s, t in MERGE.items() if t == tgt]
        print(f"\n{', '.join(srcs)} -> {tgt}")
        for s in srcs:
            lines = [ln for (fl, _, _), ln in before.items() if fl == key(s)
                     and (abs(ln["kg"]) >= TINY or abs(ln["units"]) >= TINY)]
            print(f"   {s:<24} {len(lines):>3} lines  {sum((ln['kg'] for ln in lines), Decimal(0)):>12,} kg"
                  f"  {sum((ln['units'] for ln in lines), Decimal(0)):>12,} units  -> 0")
        tl = [ln for (fl, _, _), ln in before.items() if fl == key(tgt)]
        into = [r for r in rows if r["floor"] == tgt]
        print(f"   {tgt:<24} {sum((ln['kg'] for ln in tl), Decimal(0)):>12,} kg before, "
              f"{len(into)} articles get a merge count")
    print(f"\n{len(rows)} count rows: {sum(1 for r in rows if r['floor'] in MERGE.values())} into the kept floors, "
          f"{sum(1 for r in rows if r['floor'] in MERGE)} zeroing the merged-away floors")


def save_csv(rows: list[dict], tag: str) -> Path:
    SCRATCH.mkdir(exist_ok=True)
    from datetime import datetime
    p = SCRATCH / f"a185_floor_merge_{tag}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    cols = ["floor", "item_name", "stock_type", "was_kg", "was_units", "want_kg", "want_units",
            "kg", "units", "item_type", "item_category", "item_subcategory", "remark"]
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return p


async def main(a) -> None:
    dsn = rds_dsn()
    print("database:", dsn.split("@", 1)[-1].split("/", 1)[0])
    conn = await asyncpg.connect(dsn, timeout=30)
    try:
        if not (a.apply or a.dry_run):
            async with conn.transaction(readonly=True):
                rows, before, day = await build(conn)
            report(rows, before)
            print("plan saved:", save_csv(rows, "plan"))
            print("\nPLAN ONLY - nothing written. --dry-run to test the writes, --apply to commit.")
            return

        tr = conn.transaction()
        await tr.start()
        try:
            await conn.execute("SET LOCAL lock_timeout = '10s'")
            # Block new postings and counts while balances are read and written.
            await conn.execute("LOCK TABLE new_stock_entries, stocktake_transactions IN SHARE ROW EXCLUSIVE MODE")
            rows, before, day = await build(conn)
            report(rows, before)
            if not rows:
                print("\nNothing on the merged-away floors - nothing to write.")
                await tr.rollback()
                return
            entry_id = "MRG" + day[3:5] + day[:2]
            authority = (f"A185 floor merge {day}: FFS Packing Area -> FG store; "
                         f"Dmart Production Area, Sorting Area -> Dmart Packing Area")
            await conn.executemany(
                """INSERT INTO new_stock_entries
                       (item_name, item_type, item_category, item_subcategory, warehouse, floor_name,
                        total_quantity, unit_uom, total_weight, entered_by, authority,
                        created_at, updated_at, stock_type, entry_id, status, is_checked,
                        verified, remark, source_kind)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 0, $8, $9, $10, now(), now(), $11, $12,
                           'submitted', FALSE, FALSE, $13, 'COUNT')""",
                [(r["item_name"], r["item_type"], r["item_category"], r["item_subcategory"], WH,
                  r["floor"], r["units"], r["kg"], ACTOR, authority, r["stock_type"], entry_id,
                  r["remark"]) for r in rows])
            problems = await check(conn, before, rows)
            if problems:
                print(f"\nCHECK FAILED ({len(problems)}) - rolled back, nothing written:")
                for p in problems[:40]:
                    print("  ", p)
                raise _Rollback
            print(f"\nchecks passed: merged-away floors at 0, kept floors hold the sums, "
                  f"every other A185 line and every article's A185 total unchanged ({len(before)} lines compared)")
            if a.dry_run:
                print("DRY RUN - rolled back, nothing written.")
                print("plan saved:", save_csv(rows, "dryrun"))
                raise _Rollback
            await tr.commit()
            print(f"\nCOMMITTED: {len(rows)} rows, entry_id {entry_id}.")
            print("record saved:", save_csv(rows, "applied"))
            kept = sorted({r["floor"] for r in rows if r["floor"] in MERGE.values()})
            print(f"\nNOTE: a COUNT entered on {', '.join(kept)} for any of these articles later TODAY "
                  f"adds to these rows (same-day counts are summed). Adjustments are fine.")
        except _Rollback:
            await tr.rollback()
        except BaseException:
            await tr.rollback()
            raise
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="write, check and roll back")
    g.add_argument("--apply", action="store_true", help="write, check and commit")
    asyncio.run(main(ap.parse_args()))
