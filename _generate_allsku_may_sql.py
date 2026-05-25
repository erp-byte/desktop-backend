"""Fetch all_sku.particulars from DB, match case-insensitively against May 18 GST file,
emit INSERT statements ONLY for particulars that don't yet exist in the DB.

No updates. No history. No DDL prereq.

Output: allsku_may18_inserts.sql
"""
import asyncio
import os
from pathlib import Path

import asyncpg
import openpyxl
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "tmp" / "GST Rate Setup CFPL & CDPL 18th May.xlsx"
OUT = ROOT / "allsku_may18_inserts.sql"


def _s(v):
    if v is None:
        return None
    x = str(v).strip()
    return x if x else None


def _f(v):
    if v is None:
        return None
    try:
        return round(float(v), 3)
    except (ValueError, TypeError):
        return None


def _norm_key(s):
    """Case-insensitive + whitespace-collapsed key for matching."""
    if not s:
        return ""
    return " ".join(str(s).strip().lower().split())


def _gst_pct(v):
    f = _f(v)
    if f is None:
        return None
    if f <= 1.0:
        f = f * 100.0
    return round(f, 2)


def sql_str(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def sql_num(v):
    if v is None:
        return "NULL"
    return str(v)


def read_may_rows():
    """Read both sheets. CDPL first so cross-sheet conflicts resolve to CDPL values."""
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    rows = []
    for sheet_name in ("CDPL", "CFPL"):
        ws = wb[sheet_name]
        hdr = None
        for row in ws.iter_rows(values_only=True):
            if hdr is None:
                hdr = [(_s(c) or "").lower() for c in row]
                continue
            if not row or not _s(row[0]):
                continue
            rec = dict(zip(hdr, row))
            rows.append({
                "particulars": _s(rec.get("particulars")),
                "item_type": (_s(rec.get("fg/rm/pm") or rec.get("fg/rm")) or "").lower() or None,
                "item_group": _s(rec.get("group")),
                "sub_group": _s(rec.get("sub-group")),
                "uom": _f(rec.get("uom")),
                "sale_group": _s(rec.get("sale group")),
                "gst": _gst_pct(rec.get("gst")),
            })
    wb.close()
    # Dedupe by normalised key (first occurrence wins)
    seen = set()
    deduped = []
    for r in rows:
        k = _norm_key(r["particulars"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)
    return deduped


async def main():
    may_rows = read_may_rows()

    conn = await asyncpg.connect(DB_URL)
    try:
        db = await conn.fetch("SELECT particulars FROM all_sku")
    finally:
        await conn.close()

    db_keys = {_norm_key(r["particulars"]) for r in db}
    print(f"[read] DB all_sku particulars: {len(db_keys)} unique (case-insensitive)")
    print(f"[read] May rows (deduped):     {len(may_rows)}")

    new_rows = [r for r in may_rows if _norm_key(r["particulars"]) not in db_keys]
    print(f"[diff] New SKUs (in May, not in DB): {len(new_rows)}")

    lines = [
        "-- ═══════════════════════════════════════════════════════════════",
        "-- New SKUs from GST Rate Setup CFPL & CDPL 18th May.xlsx",
        "-- Case-insensitive match against all_sku.particulars",
        f"-- {len(new_rows)} rows to insert",
        "-- ═══════════════════════════════════════════════════════════════",
        "",
        "BEGIN;",
        "",
    ]
    for r in new_rows:
        lines.append(
            "INSERT INTO all_sku (particulars, item_type, item_group, sub_group, uom, sale_group, gst, created_at) "
            f"VALUES ({sql_str(r['particulars'])}, {sql_str(r['item_type'])}, {sql_str(r['item_group'])}, "
            f"{sql_str(r['sub_group'])}, {sql_num(r['uom'])}, {sql_str(r['sale_group'])}, {sql_num(r['gst'])}, NOW());"
        )
    lines.append("")
    lines.append("COMMIT;")
    lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[ok] wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
