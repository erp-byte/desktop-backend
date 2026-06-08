"""Upsert all_sku from "All_Sku Update CFPL & CDPL 04th June" workbook.

Keyed on case-insensitive particulars. Inserts new SKUs, updates changed
attributes on existing rows, NEVER deletes (sku_id is FK-referenced by ~21
columns). Normalizes to the DB's existing conventions:
  - item_type / item_group / sub_group / sale_group  -> lowercase
  - gst                                              -> fraction (0.18, not 18)

Backs up the full table to a timestamped CSV before mutating. Transactional.
Pass --apply to write; default is a dry run (prints the plan only).
"""
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import openpyxl
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]
ROOT = Path(__file__).resolve().parent.parent
SRC = Path(r"C:\Users\Lenovo\Downloads\All_SKU Updated List\All_Sku Update CFPL & CDPL 04th June (1).xlsx")
FIELDS = ["item_type", "item_group", "sub_group", "uom", "sale_group", "gst"]


def s(v):
    if v is None:
        return None
    x = str(v).strip()
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
        db = await conn.fetch(
            "SELECT sku_id, particulars, item_type, item_group, sub_group, uom, sale_group, gst FROM all_sku"
        )
        dbm = {nk(r["particulars"]): r for r in db}

        inserts, updates = [], []
        for r in file_rows:
            k = nk(r["particulars"])
            if k not in dbm:
                inserts.append(r)
            else:
                changed = diff_fields(r, dbm[k])
                if changed:
                    updates.append((dbm[k]["sku_id"], r, changed))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = ROOT / "scratch" / f"all_sku_backup_{stamp}.csv"
        backup.parent.mkdir(exist_ok=True)
        with backup.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["sku_id", "particulars", "item_type", "item_group",
                        "sub_group", "uom", "sale_group", "gst"])
            for r in db:
                w.writerow([r["sku_id"], r["particulars"], r["item_type"], r["item_group"],
                            r["sub_group"], r["uom"], r["sale_group"], r["gst"]])

        print(f"[backup] {len(db)} rows -> {backup}")
        print(f"[plan] INSERT {len(inserts)} new SKU(s)")
        print(f"[plan] UPDATE {len(updates)} existing SKU(s)")
        for sku_id, r, changed in updates:
            print(f"        #{sku_id} {r['particulars'][:45]!r}: {changed}")

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
            for sku_id, r, _changed in updates:
                await conn.execute(
                    "UPDATE all_sku SET item_type=$2, item_group=$3, sub_group=$4, uom=$5, sale_group=$6, gst=$7"
                    " WHERE sku_id=$1",
                    sku_id, r["item_type"], r["item_group"], r["sub_group"],
                    r["uom"], r["sale_group"], r["gst"],
                )
        total = await conn.fetchval("SELECT count(*) FROM all_sku")
        print(f"\n[done] inserted {len(inserts)}, updated {len(updates)}. all_sku now has {total} rows.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
