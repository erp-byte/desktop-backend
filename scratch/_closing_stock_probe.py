"""
Closing-stock data-source discovery probe.

For each candidate table, report:
  - exists?
  - row count
  - all columns (name, type)
  - candidate date columns (any timestamp/date column) with MIN/MAX
  - row count in window [2026-04-01, today]
"""
import asyncio
import os
import sys
from datetime import date
from dotenv import load_dotenv
import asyncpg

load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

WINDOW_START = date(2026, 4, 1)
WINDOW_END = date(2026, 5, 18)

CANDIDATES = {
    "Inward": [
        "po_box", "po_line", "po_header", "po_items", "po_item_boxes",
        "inventory_batch",
        "cfpl_transactions_v2", "cdpl_transactions_v2",
        "cfpl_articles_v2", "cdpl_articles_v2",
        "cfpl_boxes_v2", "cdpl_boxes_v2",
        "grn", "grn_line", "jb_inward_boxes",
    ],
    "Transfers": [
        "interunit_transfers_header", "interunit_transfers_lines",
        "interunit_transfer_in_header", "interunit_transfer_in_boxes",
        "interunit_transfer_in_cold_storage",
        "inter_entity_transfer", "inter_entity_transfer_line",
        "cst_transferout", "cst_transferout_items",
        "inner_cold_transfer",
    ],
    "BOM_Consumption": [
        "job_card_material_consumption", "job_card_rm_indent", "job_card_pm_indent",
        "job_card_byproduct", "job_card_output",
        "prd_jc_rm", "prd_jc_pm",
        "production_consumption",
        "issue_note", "issue_note_line",
        "material_document", "material_document_line",
    ],
    "RTV": [
        "cfpl_rtv_header", "cfpl_rtv_lines", "cfpl_rtv_boxes",
        "cdpl_rtv_header", "cdpl_rtv_lines", "cdpl_rtv_boxes",
        "cfpl_complaints", "cdpl_complaints",
        "sales_outward", "billed_articles", "billed_details",
        "fulfillment_floor_stock", "sales_data",
    ],
    "Outward": [
        "cfpl_outward", "cdpl_outward", "outward_boxes", "outward_articles",
        "sales_outward", "sales_data", "daily_sales", "mis_sales",
        "billed_articles", "billed_details",
        "sales_orders", "sales_order_items",
    ],
}

# flatten unique
ALL_TABLES = sorted({t for v in CANDIDATES.values() for t in v})

OUT = []


def w(s=""):
    OUT.append(str(s))


async def main():
    conn = await asyncpg.connect(DB_URL, timeout=30)
    w(f"# Closing-stock probe — window {WINDOW_START} → {WINDOW_END}\n")

    # 1) Find which candidate tables actually exist
    rows = await conn.fetch(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_name = ANY($1::text[])
          AND table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY table_name
        """,
        ALL_TABLES,
    )
    existing = {(r["table_schema"], r["table_name"]) for r in rows}
    existing_names = {r["table_name"] for r in rows}
    missing = sorted(set(ALL_TABLES) - existing_names)
    w(f"## Existence check")
    w(f"- Found: {len(existing_names)} / {len(ALL_TABLES)}")
    w(f"- Missing: {missing}\n")

    # 2) Discover potentially-related tables (sku/qty/box/article/transfer/rtv/outward)
    extra_rows = await conn.fetch(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog','information_schema')
          AND (
            table_name ILIKE '%inward%'
            OR table_name ILIKE '%outward%'
            OR table_name ILIKE '%transfer%'
            OR table_name ILIKE '%rtv%'
            OR table_name ILIKE '%consumption%'
            OR table_name ILIKE '%job_card%'
            OR table_name ILIKE '%jc_%'
            OR table_name ILIKE '%grn%'
            OR table_name ILIKE '%dispatch%'
            OR table_name ILIKE '%sales%'
            OR table_name ILIKE '%billed%'
            OR table_name ILIKE '%material_doc%'
            OR table_name ILIKE '%complaint%'
            OR table_name ILIKE '%indent%'
            OR table_name ILIKE '%issue_note%'
            OR table_name ILIKE '%boxes_v2%'
            OR table_name ILIKE '%articles_v2%'
            OR table_name ILIKE '%transactions_v2%'
            OR table_name ILIKE '%po_%'
            OR table_name ILIKE '%fulfillment%'
          )
        ORDER BY table_name
        """,
    )
    all_related = sorted({r["table_name"] for r in extra_rows})
    not_in_candidates = sorted(set(all_related) - set(ALL_TABLES))
    w(f"## Related tables discovered (not in candidate list)")
    w(f"({len(not_in_candidates)} extras)")
    for t in not_in_candidates:
        w(f"  - {t}")
    w("")

    # 3) For each existing candidate, fetch columns + sample
    target_tables = sorted(existing_names) + not_in_candidates

    # column dump
    col_rows = await conn.fetch(
        """
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_name = ANY($1::text[])
          AND table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY table_schema, table_name, ordinal_position
        """,
        target_tables,
    )
    cols_by_table = {}
    for r in col_rows:
        cols_by_table.setdefault((r["table_schema"], r["table_name"]), []).append(
            (r["column_name"], r["data_type"])
        )

    DATE_TYPES = {"date", "timestamp without time zone", "timestamp with time zone"}

    w("## Per-table inspection\n")
    for (schema, table), cols in sorted(cols_by_table.items()):
        fq = f'"{schema}"."{table}"'
        w(f"### {schema}.{table}")
        # row count
        try:
            cnt = await conn.fetchval(f"SELECT count(*) FROM {fq}")
        except Exception as e:
            w(f"  ERROR counting: {e}")
            w("")
            continue
        w(f"  rows: {cnt}")
        # column list
        col_strs = [f"{n}:{t}" for (n, t) in cols]
        w(f"  cols ({len(cols)}): {', '.join(col_strs)}")

        if cnt == 0:
            w(f"  (empty)\n")
            continue

        # find date-ish columns
        date_cols = [n for (n, t) in cols if t in DATE_TYPES]
        # also names containing date / created / received / shipped / posting
        name_date_cols = [n for (n, t) in cols
                          if any(k in n.lower() for k in
                                 ["date","created","received","shipped","posting","entry","dispatch","trans","time","at"])]
        date_cols = list({*date_cols, *name_date_cols})
        # only keep those that actually exist as date types or text containing 'date'
        for dc in date_cols[:]:
            col_type = next((t for (n, t) in cols if n == dc), None)
            if col_type not in DATE_TYPES and col_type not in ("date", "text", "character varying"):
                date_cols.remove(dc)

        if not date_cols:
            w(f"  date_cols: (none)")
        else:
            # query min/max + window count for each candidate date col
            for dc in date_cols:
                col_type = next((t for (n, t) in cols if n == dc), None)
                try:
                    if col_type in DATE_TYPES:
                        row = await conn.fetchrow(
                            f'SELECT MIN("{dc}") AS mn, MAX("{dc}") AS mx, '
                            f'count(*) FILTER (WHERE "{dc}"::date BETWEEN $1 AND $2) AS wcnt '
                            f"FROM {fq}",
                            WINDOW_START, WINDOW_END,
                        )
                    else:
                        # skip text date cols for window calc (might fail to cast)
                        row = await conn.fetchrow(
                            f'SELECT MIN("{dc}") AS mn, MAX("{dc}") AS mx, '
                            f'NULL::int AS wcnt FROM {fq}'
                        )
                    w(f"  date_col[{dc} {col_type}]: min={row['mn']} max={row['mx']} window_rows={row['wcnt']}")
                except Exception as e:
                    w(f"  date_col[{dc} {col_type}]: ERROR {e}")

        # peek a sample row to see the shape
        try:
            sample = await conn.fetchrow(f"SELECT * FROM {fq} ORDER BY 1 DESC LIMIT 1")
            if sample:
                # only show non-null short values
                kv = []
                for k, v in sample.items():
                    sv = str(v)
                    if len(sv) > 60:
                        sv = sv[:57] + "..."
                    kv.append(f"{k}={sv}")
                w(f"  sample: {{{', '.join(kv)}}}")
        except Exception as e:
            w(f"  sample: ERROR {e}")
        w("")

    await conn.close()
    out_path = r"D:\Consumption\New\Backend\_closing_stock_probe.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))
    print(f"wrote {out_path} ({len(OUT)} lines)")


if __name__ == "__main__":
    asyncio.run(main())
