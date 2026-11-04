"""Sync all_sku.gst from Tally's "GST Rate Setup" export (Iteam Master.xls, 8 Oct 2026).

The file is Tally's Rate-wise GST report, not the IMS All_SKU workbook the June and
July scripts read: one row per stock item with GST rate, taxability and HSN/SAC,
grouped under section rows ("GST Rate-18%", "Nil Rated", ...). It carries no
item_type / group / sub_group / uom / sale_group, so the ONLY all_sku field it can
speak for is gst. HSN/SAC has no all_sku column and is ignored.

Despite the .xls name the file is an xlsx (zip) workbook; openpyxl rejects it by
extension, so it is opened from bytes.

Same contract as update_all_sku_28july.py where it applies:
  - keyed on whitespace-collapsed, case-insensitive particulars, every row sharing
    a key is updated (all_sku has duplicate particulars);
  - gst stored as a fraction (0.18, not 18);
  - NEVER deletes; never sets gst to NULL.
Decisions for this run (8 Oct 2026):
  - Nil Rated / Exempt / 0% / "Details Not Provided" rows carry no rate and leave
    gst untouched -- no all_sku row holds gst = 0 and nothing was checked to treat
    0 and NULL alike;
  - Tally items absent from all_sku are INSERTED with particulars + gst only
    (item_type etc. NULL), service/expense ledgers included, as requested.

Backs up the full table to a timestamped CSV before mutating and writes an audit
CSV (inserted sku_ids, gst old -> new) after, so the run can be reversed.
Transactional. Pass --apply to write; default is a dry run (prints the plan only).
"""
import asyncio
import csv
import io
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import openpyxl
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"].strip().replace("postgresql+asyncpg://", "postgresql://")
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "Iteam Master.xls"
COLS = ["sku_id", "particulars", "item_type", "item_group", "sub_group", "uom",
        "sale_group", "gst", "sfg_code", "created_at"]


def s(v):
    """Collapse whitespace. Tally also leaks '_x000D_' (an escaped CR) into names."""
    if v is None:
        return None
    x = " ".join(str(v).replace("_x000D_", " ").split())
    return x or None


def nk(x):
    return " ".join(str(x).strip().lower().split()) if x else ""


def unparen(v):
    """Tally brackets a value inherited from the stock group: '(18 %)' -> '18 %'."""
    x = s(v)
    if x and x.startswith("(") and x.endswith(")"):
        return x[1:-1].strip() or None
    return x


def gstf(v):
    """'18 %' / '(5 %)' -> 0.18 / 0.05. Blank -> None. Anything else is an error,
    not a silent None, so a format change in the export cannot null out a rate."""
    x = unparen(v)
    if not x:
        return None
    m = re.fullmatch(r"([\d.]+)\s*%", x)
    if not m:
        raise ValueError(f"unrecognised GST rate {v!r}")
    return round(float(m.group(1)) / 100.0, 3)


def read_rows(path=SRC):
    """Item rows of the report. Section rows sit at indent 0, items are indented."""
    wb = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), data_only=True)
    ws = wb.worksheets[0]
    rows, section, started = [], None, False
    for row in ws.iter_rows():
        name = s(row[0].value)
        if not started:
            # header is 'Particulars' followed by a sub-header row with a blank name
            started = name == "Particulars"
            continue
        if not name:
            continue
        if not (row[0].alignment and row[0].alignment.indent):
            section = name
            continue
        rows.append({"particulars": name, "section": section, "gst": gstf(row[3].value)})
    wb.close()
    if not started:
        raise ValueError(f"{path.name}: no 'Particulars' header row -- not a GST Rate Setup export?")

    by_key = defaultdict(list)
    for r in rows:
        by_key[nk(r["particulars"])].append(r)
    deduped, conflicts = [], []
    for group in by_key.values():
        if len({r["gst"] for r in group}) > 1:
            conflicts.append(group)
        else:
            deduped.append(group[0])
    return rows, deduped, conflicts


def fnum(x):
    return None if x is None else round(float(x), 3)


async def main(apply):
    raw, file_rows, conflicts = read_rows()
    print(f"[target] {urlparse(DB_URL).hostname}{urlparse(DB_URL).path}")
    conn = await asyncpg.connect(DB_URL)
    try:
        db = await conn.fetch(f"SELECT {', '.join(COLS)} FROM all_sku ORDER BY sku_id")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = ROOT / "scratch"
        out_dir.mkdir(exist_ok=True)
        backup = out_dir / f"all_sku_backup_{stamp}.csv"
        with backup.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COLS)
            for r in db:
                w.writerow([r[c] for c in COLS])
        print(f"[backup] {len(db)} rows -> {backup}")

        dbm = defaultdict(list)
        for r in db:
            dbm[nk(r["particulars"])].append(r)

        inserts, updates, blank_kept = [], [], 0
        for r in file_rows:
            group = dbm.get(nk(r["particulars"]))
            if not group:
                inserts.append(r)
                continue
            for db_row in group:
                if r["gst"] is None:
                    blank_kept += 1          # never NULL a value, never invent a 0
                elif fnum(db_row["gst"]) != r["gst"]:
                    updates.append((db_row["sku_id"], db_row["particulars"], db_row["item_type"],
                                    fnum(db_row["gst"]), r["gst"]))

        matched_rows = sum(len(dbm[nk(r["particulars"])]) for r in file_rows if nk(r["particulars"]) in dbm)
        print(f"[file]   {len(raw)} item rows, {len(file_rows)} unique after dedupe")
        if conflicts:
            print(f"[warn]   {len(conflicts)} name(s) carry conflicting rates in the file and are SKIPPED:")
            for g in conflicts:
                print(f"         {g[0]['particulars']!r}: {[(x['section'], x['gst']) for x in g]}")
        print(f"[plan]   UPDATE gst on {len(updates)} row(s) "
              f"({matched_rows - len(updates) - blank_kept} already equal, "
              f"{blank_kept} left as-is: Nil Rated / Exempt / 0% / no rate in Tally)")
        for sku_id, p, it, old, new in updates:
            print(f"         #{sku_id} [{it}] {p[:60]!r}: {old} -> {new}")
        print(f"[plan]   INSERT {len(inserts)} new SKU(s) (particulars + gst only)")
        by_sec = defaultdict(int)
        for r in inserts:
            by_sec[r["section"]] += 1
        print(f"         by Tally section: {dict(by_sec)}")
        print(f"[plan]   LEAVE  {sum(len(v) for k, v in dbm.items() if k not in {nk(r['particulars']) for r in file_rows})}"
              " DB row(s) not present in the file (never deleted)")

        seq_last = await conn.fetchval("SELECT last_value FROM all_sku_sku_id_seq")
        clash = await conn.fetchval(
            "SELECT count(*) FROM all_sku WHERE sku_id > $1 AND sku_id <= $1 + $2", seq_last, len(inserts))
        print(f"[check]  sku_id sequence at {seq_last}; existing ids in the next {len(inserts)}: {clash}")
        if clash:
            raise SystemExit("[abort]  the sequence would hand out sku_ids that already exist")

        if not apply:
            print("\n[dry-run] no changes written. Re-run with --apply to commit.")
            return

        audit_rows = []
        async with conn.transaction():
            for sku_id, p, it, old, new in updates:
                await conn.execute("UPDATE all_sku SET gst=$2 WHERE sku_id=$1", sku_id, new)
                audit_rows.append(["UPDATE", sku_id, p, old, new])
            for r in inserts:
                new_id = await conn.fetchval(
                    "INSERT INTO all_sku (particulars, gst, created_at) VALUES ($1, $2, NOW()) RETURNING sku_id",
                    r["particulars"], r["gst"],
                )
                audit_rows.append(["INSERT", new_id, r["particulars"], None, r["gst"]])

        audit = out_dir / f"all_sku_gst_08oct_audit_{stamp}.csv"
        with audit.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["action", "sku_id", "particulars", "gst_old", "gst_new"])
            w.writerows(audit_rows)
        total = await conn.fetchval("SELECT count(*) FROM all_sku")
        print(f"\n[done] updated {len(updates)}, inserted {len(inserts)}. all_sku now has {total} rows.")
        print(f"[audit] {audit}")
    finally:
        await conn.close()


def selfcheck():
    """The bits that would silently corrupt data if they broke."""
    assert gstf("18 %") == 0.18 and gstf("(5 %)") == 0.05 and gstf("1 %") == 0.01
    assert gstf("") is None and gstf(None) is None and gstf("()") is None
    try:
        gstf("Taxable")
    except ValueError:
        pass
    else:
        raise AssertionError("a non-rate must not parse as blank")
    assert s("CALIBRATION CHARGES_x000D_\n") == "CALIBRATION CHARGES"
    assert nk("  Cashew\xa0 W320 ") == "cashew w320"
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        asyncio.run(main(apply="--apply" in sys.argv))
