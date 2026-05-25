"""
Read-only audit probe.
Runs the queries spelled out in the audit task brief and writes raw output
to _audit_db_dump.txt for later synthesis.

Does NOT write to the database.
"""
import asyncio
import os
import sys
import traceback
from pathlib import Path

import asyncpg

DB_URL = "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db"
DUMP = Path(r"D:\Consumption\New\Backend\_audit_db_dump.txt")


QUERIES = [
    ("01_tables_with_row_counts", """
        SELECT schemaname, relname AS table_name, n_live_tup AS row_count
        FROM pg_stat_user_tables
        ORDER BY schemaname, relname;
    """),
    ("02_table_sizes", """
        SELECT
          n.nspname AS schema, c.relname AS "table",
          pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
          pg_size_pretty(pg_relation_size(c.oid)) AS heap,
          pg_size_pretty(pg_indexes_size(c.oid)) AS indexes,
          pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema')
        ORDER BY pg_total_relation_size(c.oid) DESC;
    """),
    ("03_columns_per_table", """
        SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY table_schema, table_name, ordinal_position;
    """),
    ("04_pks_and_fks", """
        SELECT tc.table_schema, tc.table_name, tc.constraint_name, tc.constraint_type,
               kcu.column_name, ccu.table_name AS ref_table, ccu.column_name AS ref_column
        FROM information_schema.table_constraints tc
        LEFT JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        LEFT JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY tc.table_schema, tc.table_name, tc.constraint_type;
    """),
    ("05_indexes", """
        SELECT schemaname, tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname NOT IN ('pg_catalog','information_schema')
        ORDER BY schemaname, tablename;
    """),
    ("06_triggers", """
        SELECT event_object_schema, event_object_table, trigger_name, event_manipulation, action_statement
        FROM information_schema.triggers ORDER BY 1,2,3;
    """),
    ("07_sequences", """
        SELECT sequence_schema, sequence_name, data_type, start_value, increment
        FROM information_schema.sequences ORDER BY 1,2;
    """),
    ("08_extensions", """
        SELECT extname, extversion FROM pg_extension ORDER BY extname;
    """),
    ("09_pg_stat_statements_top20", """
        SELECT query, calls, total_exec_time, mean_exec_time, rows
        FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
    """),
    ("10_tables_no_pk", """
        SELECT n.nspname, c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind='r' AND n.nspname='public'
          AND NOT EXISTS (
            SELECT 1 FROM pg_constraint con WHERE con.conrelid=c.oid AND con.contype='p'
          );
    """),
    ("11_unindexed_fk_cols", """
        WITH fkeys AS (
          SELECT con.conrelid::regclass AS tbl, a.attname AS col, con.conname
          FROM pg_constraint con
          JOIN pg_attribute a ON a.attrelid=con.conrelid AND a.attnum = ANY(con.conkey)
          WHERE con.contype='f'
        )
        SELECT f.tbl::text AS tbl, f.col, f.conname FROM fkeys f
        WHERE NOT EXISTS (
          SELECT 1 FROM pg_index i
          JOIN pg_attribute ia ON ia.attrelid=i.indrelid AND ia.attnum = ANY(i.indkey)
          WHERE i.indrelid = f.tbl::regclass AND ia.attname = f.col
        )
        ORDER BY 1,2;
    """),
    ("12a_all_sku_columns", """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name='all_sku' ORDER BY ordinal_position;
    """),
    ("12b_all_sku_count", "SELECT count(*) AS cnt FROM all_sku;"),
    ("12c_log_edit_count", "SELECT count(*) AS cnt FROM log_edit;"),
    ("12d_po_line_status_breakdown", "SELECT status, count(*) AS cnt FROM po_line GROUP BY status;"),
]


# Probes for the "8 free-text customer reference" hunt.
EXTRA_QUERIES = [
    ("13_customer_text_columns", """
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema NOT IN ('pg_catalog','information_schema')
          AND (
            column_name ILIKE '%customer%'
            OR column_name ILIKE '%client%'
            OR column_name ILIKE '%party%'
            OR column_name ILIKE '%buyer%'
          )
        ORDER BY table_schema, table_name, column_name;
    """),
    ("14_columns_named_customer_id", """
        SELECT table_schema, table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE column_name='customer_id'
          AND table_schema NOT IN ('pg_catalog','information_schema')
        ORDER BY table_schema, table_name;
    """),
    ("15_designed_table_existence", """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog','information_schema')
          AND table_name IN (
            'po_header','po_line','po_box',
            'coa_document',
            'qc_inspection','qc_parameter_master','qc_parameter_spec','qc_inspection_parameter',
            'ncr_record','ncr_parameter_detail','ncr_supplier_action',
            'stage2_approval','grn',
            'receipt_event_log','notification_log',
            'all_sku','log_edit'
          )
        ORDER BY table_schema, table_name;
    """),
    ("16_po_line_full_columns", """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name='po_line'
        ORDER BY ordinal_position;
    """),
    ("17_po_header_full_columns", """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name='po_header'
        ORDER BY ordinal_position;
    """),
    ("18_po_box_full_columns", """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name='po_box'
        ORDER BY ordinal_position;
    """),
    ("19_all_schemas", """
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast')
        ORDER BY schema_name;
    """),
    ("20_db_version", "SELECT version();"),
    ("21_current_database", "SELECT current_database();"),
]


def fmt_rows(rows):
    if not rows:
        return "(no rows)\n"
    cols = list(rows[0].keys())
    out_lines = []
    out_lines.append(" | ".join(cols))
    out_lines.append("-" * 80)
    for r in rows:
        out_lines.append(" | ".join("" if r[c] is None else str(r[c]) for c in cols))
    out_lines.append(f"({len(rows)} rows)")
    return "\n".join(out_lines) + "\n"


async def main():
    dump_lines = []
    dump_lines.append("=" * 80)
    dump_lines.append("CANDOR PRODUCTION DB AUDIT DUMP")
    dump_lines.append("=" * 80)
    dump_lines.append(f"DSN: postgresql://wmsadmin:***@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db")
    dump_lines.append("")

    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(DB_URL, timeout=30),
            timeout=30,
        )
    except Exception as exc:
        msg = f"CONNECTION FAILED: {type(exc).__name__}: {exc}"
        dump_lines.append(msg)
        dump_lines.append(traceback.format_exc())
        DUMP.write_text("\n".join(dump_lines), encoding="utf-8")
        print(msg)
        return 1

    dump_lines.append("CONNECTION: OK")
    dump_lines.append("")

    all_queries = QUERIES + EXTRA_QUERIES
    for name, sql in all_queries:
        dump_lines.append("=" * 80)
        dump_lines.append(f"# {name}")
        dump_lines.append("-" * 80)
        dump_lines.append(sql.strip())
        dump_lines.append("-" * 80)
        try:
            rows = await conn.fetch(sql)
            dump_lines.append(fmt_rows(rows))
        except Exception as exc:
            dump_lines.append(f"ERROR: {type(exc).__name__}: {exc}")
        dump_lines.append("")

    await conn.close()
    DUMP.write_text("\n".join(dump_lines), encoding="utf-8")
    print(f"Dump written: {DUMP} ({DUMP.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
