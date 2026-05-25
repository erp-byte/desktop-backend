"""One-off: regenerate savla_articles_backfill.sql with Rishi legacy inserts."""
import openpyxl
import re
from pathlib import Path
from collections import Counter

wb = openpyxl.load_workbook("Savla_Rishi_Inventory_12th May 2026.xlsx", read_only=True, data_only=True)
ws = wb["12th May 26"]


def sql_escape(s):
    if s is None:
        return "NULL"
    s = str(s).strip()
    if not s:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def num_or_null(v):
    if v is None or v == "":
        return "NULL"
    try:
        return str(float(v))
    except (ValueError, TypeError):
        return "NULL"


def date_or_null(v):
    if v is None:
        return "NULL"
    if hasattr(v, "strftime"):
        return "'" + v.strftime("%Y-%m-%d") + "'"
    return "NULL"


HEADER_ROW = 6
rows = list(ws.iter_rows(values_only=True))
header = rows[HEADER_ROW - 1]


def find_col(name, occurrence=0):
    matches = [i for i, h in enumerate(header) if h and str(h).strip().lower() == name.lower()]
    return matches[occurrence]


C_ITEM_MARK = find_col("Item Mark", 0)
C_INWARD_DT = find_col("Inward Dt")
C_INWARD_NO = find_col("Inward No")
C_UNIT = find_col("Unit")
C_LOT = find_col("Lot No")
C_ITEM_DESC = find_col("Item Description")
C_NET_QTY = find_col("Net Qty On Cartons")
C_WEIGHT_KG = find_col("Weight KG")
C_TOTAL_INV = find_col("Total Inventory Kgs")
C_SPL_REMARKS = find_col("Spl. Remarks")
C_COMPANY = find_col("Company Name")
C_STORAGE = find_col("Storage Location")
C_EXPORTER = find_col("Exporter")
C_RATE = find_col("Last Purchase Rate")

gr_pattern = re.compile(r"^GR\d+$")
canonical = {"CDPL": [], "CFPL": []}
rishi_rows = []
other_anomalies = []

for idx, r in enumerate(rows[HEADER_ROW:], start=HEADER_ROW + 1):
    if not r or r[C_INWARD_NO] is None:
        continue
    inward = str(r[C_INWARD_NO]).strip()
    company = (r[C_COMPANY] or "").strip().upper()
    rec = {
        "row": idx,
        "transaction_no": inward,
        "item_description": str(r[C_ITEM_DESC] or "").strip(),
        "item_mark": r[C_ITEM_MARK],
        "spl_remarks": r[C_SPL_REMARKS],
        "unit": r[C_UNIT],
        "lot_no": r[C_LOT],
        "net_qty_cartons": r[C_NET_QTY],
        "weight_kg": r[C_WEIGHT_KG],
        "total_inv_kg": r[C_TOTAL_INV],
        "storage": r[C_STORAGE],
        "exporter": r[C_EXPORTER],
        "rate": r[C_RATE],
        "inward_dt": r[C_INWARD_DT],
        "company": company,
    }
    if gr_pattern.match(inward) and company in canonical:
        canonical[company].append(rec)
    elif inward == "Rishi Cold":
        rishi_rows.append(rec)
    else:
        other_anomalies.append(rec)

lot_counts = Counter(str(r["lot_no"]) for r in rishi_rows)
seen = Counter()
for r in rishi_rows:
    lot = str(r["lot_no"]) if r["lot_no"] not in (None, "") else "ROW" + str(r["row"])
    if lot_counts[lot] > 1:
        seen[lot] += 1
        r["synth_txn"] = "RISHI-LEGACY-" + lot + "-" + str(seen[lot])
    else:
        r["synth_txn"] = "RISHI-LEGACY-" + lot

key_counts = Counter((r["synth_txn"], r["item_description"]) for r in rishi_rows)
dup_keys = [k for k, c in key_counts.items() if c > 1]
print("Rishi rows:", len(rishi_rows), "| unique keys:", len(key_counts), "| dups:", len(dup_keys))

sql_lines = []
sql_lines.append("-- =========================================================================")
sql_lines.append("-- Backfill item_mark and spl_remarks on {cdpl,cfpl}_bulk_entry_articles")
sql_lines.append("-- Source: Savla_Rishi_Inventory_12th May 2026.xlsx, sheet '12th May 26'")
sql_lines.append("--")
sql_lines.append("-- Part 1: Schema additions (idempotent ALTER)")
sql_lines.append("-- Part 2: UPDATE canonical GR-prefixed rows (50 CDPL + 346 CFPL)")
sql_lines.append("-- Part 3: INSERT Rishi Cold Storage legacy rows with synthetic transaction_no")
sql_lines.append("--         (84 rows). Idempotent via WHERE NOT EXISTS.")
sql_lines.append("--         Seeds parent cdpl_bulk_entry_transactions rows first.")
sql_lines.append("-- =========================================================================")
sql_lines.append("BEGIN;")
sql_lines.append("")
sql_lines.append("-- Part 1: Schema additions")
sql_lines.append("ALTER TABLE cdpl_bulk_entry_articles ADD COLUMN IF NOT EXISTS item_mark   VARCHAR;")
sql_lines.append("ALTER TABLE cdpl_bulk_entry_articles ADD COLUMN IF NOT EXISTS spl_remarks VARCHAR;")
sql_lines.append("ALTER TABLE cfpl_bulk_entry_articles ADD COLUMN IF NOT EXISTS item_mark   VARCHAR;")
sql_lines.append("ALTER TABLE cfpl_bulk_entry_articles ADD COLUMN IF NOT EXISTS spl_remarks VARCHAR;")
sql_lines.append("")


def emit_update(company_lower, rows_list):
    parts = []
    parts.append("-- " + str(len(rows_list)) + " canonical rows for " + company_lower.upper())
    parts.append("UPDATE " + company_lower + "_bulk_entry_articles AS a SET")
    parts.append("    item_mark   = COALESCE(v.item_mark,   a.item_mark),")
    parts.append("    spl_remarks = COALESCE(v.spl_remarks, a.spl_remarks)")
    parts.append("FROM (VALUES")
    vals = []
    for r in rows_list:
        vals.append(
            "    ("
            + sql_escape(r["transaction_no"]) + ", "
            + sql_escape(r["item_description"]) + ", "
            + sql_escape(r["item_mark"]) + ", "
            + sql_escape(r["spl_remarks"])
            + ")"
        )
    parts.append(",\n".join(vals))
    parts.append(") AS v(transaction_no, item_description, item_mark, spl_remarks)")
    parts.append("WHERE a.transaction_no   = v.transaction_no")
    parts.append("  AND a.item_description = v.item_description;")
    return "\n".join(parts)


sql_lines.append("-- Part 2: UPDATE canonical rows")
sql_lines.append("")
sql_lines.append(emit_update("cdpl", canonical["CDPL"]))
sql_lines.append("")
sql_lines.append(emit_update("cfpl", canonical["CFPL"]))
sql_lines.append("")

sql_lines.append("-- Part 3: INSERT Rishi Cold Storage legacy rows (synthetic transaction_no)")
sql_lines.append("")
sql_lines.append("-- 3a. Seed parent transactions (one row per unique synthetic txn)")
unique_txns = {}
for r in rishi_rows:
    if r["synth_txn"] not in unique_txns:
        unique_txns[r["synth_txn"]] = r

sql_lines.append("INSERT INTO cdpl_bulk_entry_transactions (")
sql_lines.append("    transaction_no, entry_date, source_location, vendor_supplier_name,")
sql_lines.append("    warehouse, remark, status")
sql_lines.append(")")
sql_lines.append("SELECT v.transaction_no, v.entry_date, v.source_location, v.vendor_supplier_name,")
sql_lines.append("       v.warehouse, v.remark, 'approved'")
sql_lines.append("FROM (VALUES")
txn_vals = []
for txn_no, r in unique_txns.items():
    remark = "Legacy Rishi Cold Storage bulk holding -- lot " + str(r["lot_no"])
    txn_vals.append(
        "    ("
        + sql_escape(txn_no) + "::varchar, "
        + date_or_null(r["inward_dt"]) + "::date, "
        + sql_escape(r["storage"]) + ", "
        + sql_escape(r["exporter"]) + ", "
        + sql_escape(r["unit"]) + ", "
        + sql_escape(remark)
        + ")"
    )
sql_lines.append(",\n".join(txn_vals))
sql_lines.append(") AS v(transaction_no, entry_date, source_location, vendor_supplier_name, warehouse, remark)")
sql_lines.append("WHERE NOT EXISTS (")
sql_lines.append("    SELECT 1 FROM cdpl_bulk_entry_transactions t WHERE t.transaction_no = v.transaction_no")
sql_lines.append(");")
sql_lines.append("")

sql_lines.append("-- 3b. Insert articles (one row per xlsx row)")
sql_lines.append("INSERT INTO cdpl_bulk_entry_articles (")
sql_lines.append("    transaction_no, item_description, item_mark, spl_remarks,")
sql_lines.append("    lot_number, net_weight, total_weight, po_quantity, unit_rate, box_count")
sql_lines.append(")")
sql_lines.append("SELECT v.transaction_no, v.item_description, v.item_mark, v.spl_remarks,")
sql_lines.append("       v.lot_number, v.net_weight, v.total_weight, v.po_quantity, v.unit_rate, 0")
sql_lines.append("FROM (VALUES")
art_vals = []
for r in rishi_rows:
    art_vals.append(
        "    ("
        + sql_escape(r["synth_txn"]) + ", "
        + sql_escape(r["item_description"]) + ", "
        + sql_escape(r["item_mark"]) + ", "
        + sql_escape(r["spl_remarks"]) + ", "
        + sql_escape(r["lot_no"]) + ", "
        + num_or_null(r["weight_kg"]) + "::numeric, "
        + num_or_null(r["total_inv_kg"]) + "::numeric, "
        + num_or_null(r["net_qty_cartons"]) + "::numeric, "
        + num_or_null(r["rate"]) + "::numeric"
        + ")"
    )
sql_lines.append(",\n".join(art_vals))
sql_lines.append(") AS v(transaction_no, item_description, item_mark, spl_remarks, lot_number,")
sql_lines.append("       net_weight, total_weight, po_quantity, unit_rate)")
sql_lines.append("WHERE NOT EXISTS (")
sql_lines.append("    SELECT 1 FROM cdpl_bulk_entry_articles a")
sql_lines.append("    WHERE a.transaction_no   = v.transaction_no")
sql_lines.append("      AND a.item_description = v.item_description")
sql_lines.append("      AND COALESCE(a.lot_number, '') = COALESCE(v.lot_number, '')")
sql_lines.append(");")
sql_lines.append("")
sql_lines.append("COMMIT;")

Path("savla_articles_backfill.sql").write_text("\n".join(sql_lines), encoding="utf-8")
sz = Path("savla_articles_backfill.sql").stat().st_size
print("Wrote savla_articles_backfill.sql:", sz, "bytes")
print("  UPDATEs:", len(canonical["CDPL"]), "CDPL +", len(canonical["CFPL"]), "CFPL =", len(canonical["CDPL"]) + len(canonical["CFPL"]))
print("  INSERTs:", len(unique_txns), "synthetic txns +", len(rishi_rows), "articles")
print("  Still excluded:", len(other_anomalies))

# Update anomalies report
rep = []
rep.append("# Remaining anomalies after option (a)")
rep.append("")
rep.append("84 Rishi Cold rows are now INCLUDED in savla_articles_backfill.sql with synthetic")
rep.append("transaction_no = 'RISHI-LEGACY-{lot_no}' (see Part 3 of the SQL file).")
rep.append("")
rep.append("## Still excluded -- needs your decision: " + str(len(other_anomalies)) + " row")
rep.append("")
for r in other_anomalies:
    rep.append("- Row " + str(r["row"]) + ": transaction_no=" + repr(r["transaction_no"])
               + ", unit=" + repr(r["unit"])
               + ", item=" + repr(r["item_description"])
               + ", lot=" + repr(r["lot_no"])
               + ", mark=" + repr(r["item_mark"])
               + ", remarks=" + repr(r["spl_remarks"]))
rep.append("")
rep.append("Inward No is a 5-digit number `23007` with no GR prefix, from a `Supreme`")
rep.append("warehouse. Treat as typo for `GR23007` and manually add, or leave excluded.")
Path("savla_articles_anomalies.md").write_text("\n".join(rep), encoding="utf-8")
print("Wrote savla_articles_anomalies.md:", Path("savla_articles_anomalies.md").stat().st_size, "bytes")
