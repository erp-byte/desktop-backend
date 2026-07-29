"""Upsert all_sku from "IMS All_SKU File CF&CD 28th July" workbook.

Same contract as update_all_sku_04june.py: keyed on whitespace-collapsed,
case-insensitive particulars; inserts new SKUs, updates changed attributes,
NEVER deletes (sku_id is FK-referenced widely). Normalizes to DB convention:
  - item_type / item_group / sub_group / sale_group  -> lowercase
  - gst                                              -> fraction (0.18, not 18)

Differs from the June script in one way that matters: all_sku currently holds
85 particulars that occur on MORE than one sku_id (527 surplus rows, mostly an
unnormalized bulk-RM ingest at sku_id>=3733 carrying gst as percent). June's
dict-of-one-row would have silently updated just the last of each group and
left its siblings stale, so this keys the DB side to a LIST and updates every
row sharing a key. 17 of those groups appear in the July file.

File columns Customer / For Inventory / HSN-SAC have no all_sku column and are
ignored; hsn_sac lives on fg_master and is loaded by sync_fg_master.py.

Backs up the full table to a timestamped CSV before mutating. Transactional.
Pass --apply to write; default is a dry run (prints the plan only).
"""
import asyncio
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import openpyxl
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"].strip()  # .strip() guards against CRLF in .env
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "IMS All_SKU File CF&CD 28th July.xlsx"
FIELDS = ["item_type", "item_group", "sub_group", "uom", "sale_group", "gst"]
COLS = ["sku_id", "particulars"] + FIELDS + ["sfg_code"]
# The IMS file has no concept of semi-finished goods: it classifies an SFG by its
# downstream identity (item_type fg/rm, sale_group va/bulk). all_sku holds 343 rows
# with item_type='sfg', every one carrying an sfg_code with a 1:1 sfg_master row.
# Letting the file win on these two fields would break that seam, so it never does.
SFG_PROTECTED = ("item_type", "sale_group")


def s(v):
    """Collapse all unicode whitespace to single spaces. The file carries NBSPs
    (\\xa0) inside 31 particulars; storing them raw would make the stored value
    disagree with the nk() key we matched on."""
    if v is None:
        return None
    x = " ".join(str(v).split())
    return x or None


def sl(v):
    x = s(v)
    return x.lower() if x else None


def f(v):
    if v is None:
        return None
    try:
        return round(float(v), 3)
    except (ValueError, TypeError):
        return None


def gstf(v):
    """GST as a fraction (DB convention). Coerce any percent value to fraction."""
    x = f(v)
    if x is None:
        return None
    if x > 1.0:
        x /= 100.0
    return round(x, 3)


def nk(x):
    return " ".join(str(x).strip().lower().split()) if x else ""


def read_rows():
    """CDPL first so first-occurrence-wins dedupe resolves cross-sheet to CDPL."""
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    rows = []
    for sheet in ("CDPL", "CFPL"):
        ws = wb[sheet]
        hdr = None
        for row in ws.iter_rows(values_only=True):
            if hdr is None:
                hdr = [(s(c) or "").lower() for c in row]
                continue
            if not row or not s(row[0]):
                continue
            rec = dict(zip(hdr, row))
            rows.append({
                "particulars": s(rec.get("particulars")),
                "item_type": sl(rec.get("fg/rm/pm") or rec.get("fg/rm")),
                "item_group": sl(rec.get("group")),
                "sub_group": sl(rec.get("sub-group")),
                "uom": f(rec.get("uom")),
                "sale_group": sl(rec.get("sale group")),
                "gst": gstf(rec.get("gst")),
            })
    wb.close()
    seen, deduped = set(), []
    for r in rows:
        k = nk(r["particulars"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return deduped


def fnum(x):
    return None if x is None else round(float(x), 3)


def diff_fields(file_row, db_row):
    out = []
    for fld in FIELDS:
        if fld in SFG_PROTECTED and db_row["sfg_code"]:
            continue
        a, b = file_row[fld], db_row[fld]
        if fld in ("uom", "gst"):
            a, b = fnum(a), fnum(b)
        if str(a) != str(b):
            out.append(fld)
    return out


async def main(apply):
    file_rows = read_rows()
    conn = await asyncpg.connect(DB_URL)
    try:
        db = await conn.fetch(f"SELECT {', '.join(COLS)} FROM all_sku ORDER BY sku_id")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = ROOT / "scratch" / f"all_sku_backup_{stamp}.csv"
        backup.parent.mkdir(exist_ok=True)
        with backup.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COLS)
            for r in db:
                w.writerow([r[c] for c in COLS])
        print(f"[backup] {len(db)} rows -> {backup}")

        dbm = defaultdict(list)
        for r in db:
            dbm[nk(r["particulars"])].append(r)

        inserts, updates, nulling, sfg_held = [], [], [], []
        for r in file_rows:
            group = dbm.get(nk(r["particulars"]))
            if not group:
                inserts.append(r)
                continue
            for db_row in group:
                if db_row["sfg_code"]:
                    for fld in SFG_PROTECTED:
                        if str(r[fld]) != str(db_row[fld]):
                            sfg_held.append((db_row["sku_id"], db_row["particulars"],
                                             db_row["sfg_code"], fld, db_row[fld], r[fld]))
                changed = diff_fields(r, db_row)
                if not changed:
                    continue
                updates.append((db_row["sku_id"], r, changed))
                for c in changed:
                    if r[c] is None and db_row[c] is not None:
                        nulling.append((db_row["sku_id"], r["particulars"], c, db_row[c]))

        touched_keys = {nk(r["particulars"]) for r in file_rows}
        sibling_rows = sum(len(v) - 1 for k, v in dbm.items() if k in touched_keys and len(v) > 1)
        untouched = sum(len(v) for k, v in dbm.items() if k not in touched_keys)

        print(f"[file]   {len(file_rows)} unique SKUs after dedupe")
        print(f"[plan]   INSERT {len(inserts)} new SKU(s)")
        print(f"[plan]   UPDATE {len(updates)} row(s) "
              f"({sibling_rows} duplicate-sibling rows are in scope)")
        print(f"[plan]   LEAVE  {untouched} DB row(s) not present in the file (never deleted)")
        if sfg_held:
            print(f"[guard]  {len(sfg_held)} SFG field change(s) SUPPRESSED "
                  f"({len({h[0] for h in sfg_held})} sku_id(s) keep their seam identity):")
            for sku_id, p, code, fld, keep, ignored in sfg_held:
                print(f"         #{sku_id} {code} {p[:34]!r}.{fld}: keeping {keep!r}, file said {ignored!r}")
        if nulling:
            print(f"[warn]   {len(nulling)} field(s) would be set NULL (file blank, DB had a value):")
            for sku_id, p, c, old in nulling[:15]:
                print(f"         #{sku_id} {p[:40]!r}.{c}: {old!r} -> NULL")
            if len(nulling) > 15:
                print(f"         ... and {len(nulling) - 15} more")

        field_hits = defaultdict(int)
        for _sid, _r, changed in updates:
            for c in changed:
                field_hits[c] += 1
        print(f"[plan]   changes by field: {dict(sorted(field_hits.items()))}")
        for sku_id, r, changed in updates[:25]:
            print(f"         #{sku_id} {r['particulars'][:45]!r}: {changed}")
        if len(updates) > 25:
            print(f"         ... and {len(updates) - 25} more")

        if not apply:
            print("\n[dry-run] no changes written. Re-run with --apply to commit.")
            return

        async with conn.transaction():
            for r in inserts:
                await conn.execute(
                    "INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at)"
                    " VALUES ($1,$2,$3,$4,$5,$6,$7,NOW())",
                    r["particulars"], r["item_type"], r["item_group"], r["sub_group"],
                    r["uom"], r["sale_group"], r["gst"],
                )
            for sku_id, r, changed in updates:
                # only the fields that actually differ, so the SFG guard in
                # diff_fields() genuinely keeps item_type/sale_group untouched
                sets = ", ".join(f"{c}=${i}" for i, c in enumerate(changed, start=2))
                await conn.execute(
                    f"UPDATE all_sku SET {sets} WHERE sku_id=$1",
                    sku_id, *[r[c] for c in changed],
                )
        total = await conn.fetchval("SELECT count(*) FROM all_sku")
        print(f"\n[done] inserted {len(inserts)}, updated {len(updates)}. all_sku now has {total} rows.")
    finally:
        await conn.close()


def selfcheck():
    """The bits that would silently corrupt data if they broke."""
    assert gstf(18) == 0.18 and gstf(0.18) == 0.18 and gstf(5) == 0.05
    assert gstf(None) is None
    assert s("PM24\xa0Pouch  A") == "PM24 Pouch A"          # NBSP collapses
    assert nk("  Cashew\xa0 W320 ") == "cashew w320"

    sfg = {"item_type": "sfg", "sale_group": "wip", "item_group": "nut",
           "sub_group": "x", "uom": 1, "gst": 0.05, "sfg_code": "SFG0019"}
    plain = dict(sfg, sfg_code=None)
    incoming = {"item_type": "fg", "sale_group": "va", "item_group": "cashew",
                "sub_group": "x", "uom": 1, "gst": 0.05}
    # seam identity is held; everything else still updates
    assert diff_fields(incoming, sfg) == ["item_group"]
    # without an sfg_code the file wins on all three
    assert diff_fields(incoming, plain) == ["item_type", "item_group", "sale_group"]
    # equal rows produce no UPDATE at all (empty SET would be invalid SQL)
    assert diff_fields(incoming, dict(plain, **incoming)) == []
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        asyncio.run(main(apply="--apply" in sys.argv))
