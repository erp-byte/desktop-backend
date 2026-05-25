"""Supplementary read-only probe: find the 8 in-scope tables that store
free-text customer references and lack a customer_id FK to customer_master."""
import asyncio
import asyncpg

DB_URL = "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db"


SQL_INSCOPE = """
-- Find every table that has a free-text customer_name / customer_party_name
-- but does NOT have a typed customer_id column.
WITH cust_text AS (
  SELECT table_name
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND column_name IN ('customer_name','customer_party_name','customer_code','customer','common_customer_name')
),
typed_id AS (
  SELECT table_name
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND column_name = 'customer_id'
)
SELECT DISTINCT t.table_name,
       array_agg(DISTINCT c.column_name) AS text_cols,
       (SELECT bool_or(table_name = t.table_name) FROM typed_id) AS has_customer_id
FROM cust_text t
JOIN information_schema.columns c ON c.table_name = t.table_name AND c.table_schema='public'
WHERE c.column_name IN ('customer_name','customer_party_name','customer_code','customer','common_customer_name')
GROUP BY t.table_name
ORDER BY t.table_name;
"""

SQL_INSCOPE_NEW = """
-- Filter narrowed to the modules being built/refactored (PO, SO, internal,
-- production planning, indents, fulfillment, store-allocation, jobcards).
SELECT table_name,
       (SELECT array_agg(column_name ORDER BY column_name)
          FROM information_schema.columns
          WHERE table_schema='public' AND table_name=t.table_name
            AND column_name IN ('customer_name','customer_party_name','customer_code','customer','common_customer_name','party_name','party_code','to_party','reserved_for_customer','blocked_for_customer')
       ) AS text_customer_cols,
       EXISTS (
         SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name=t.table_name AND column_name='customer_id'
       ) AS has_customer_id_column,
       EXISTS (
         SELECT 1
           FROM information_schema.table_constraints tc
           JOIN information_schema.key_column_usage kcu
             ON tc.constraint_name = kcu.constraint_name AND tc.table_schema=kcu.table_schema
           JOIN information_schema.constraint_column_usage ccu
             ON tc.constraint_name = ccu.constraint_name
          WHERE tc.constraint_type='FOREIGN KEY'
            AND kcu.table_name = t.table_name
            AND kcu.column_name = 'customer_id'
            AND ccu.table_name = 'customer_master'
       ) AS customer_id_fk_present
FROM information_schema.tables t
WHERE t.table_schema='public'
  AND t.table_type='BASE TABLE'
  AND t.table_name IN (
    'po_header','po_line','so_header','so_line','so_fulfillment','so_revision_log',
    'production_indent','production_order','production_plan_line','production_plan',
    'purchase_indent','purchase_orders','purchase_approvals',
    'job_card','prd_jc','prd_order','internal_order','internal_issue_note',
    'lot_block','store_allocation','customer_tolerance','issue_note',
    'jb_materialout_header','bom_header','sales_orders','outward_articles',
    'cdpl_outward','cfpl_outward','customer_master','mst_customer'
  )
  AND EXISTS (
    SELECT 1 FROM information_schema.columns c
     WHERE c.table_schema='public' AND c.table_name=t.table_name
       AND c.column_name IN ('customer_name','customer_party_name','customer_code','customer','common_customer_name','party_name','party_code','to_party','reserved_for_customer','blocked_for_customer')
  )
ORDER BY table_name;
"""

SQL_FK_TO_CUSTOMER_MASTER = """
SELECT tc.table_name AS from_table, kcu.column_name AS from_column,
       ccu.table_name AS to_table, ccu.column_name AS to_column,
       tc.constraint_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND ccu.table_name IN ('customer_master','mst_customer','customers')
  AND tc.table_schema='public'
ORDER BY tc.table_name;
"""

SQL_PO_LINE_DISTINCT_STATUS = "SELECT DISTINCT status FROM po_line;"

SQL_PO_LINE_STATUS_CHK = """
SELECT pg_get_constraintdef(c.oid) AS def
  FROM pg_constraint c
  JOIN pg_class r ON r.oid = c.conrelid
 WHERE r.relname = 'po_line' AND c.contype = 'c';
"""

SQL_SUPPLIER_FKS = """
SELECT tc.table_name, kcu.column_name, ccu.table_name as ref_table, ccu.column_name as ref_column
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name AND tc.table_schema=kcu.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON tc.constraint_name = ccu.constraint_name AND tc.table_schema=ccu.table_schema
 WHERE tc.constraint_type='FOREIGN KEY'
   AND ccu.table_name='vendor_master'
   AND tc.table_schema='public'
ORDER BY tc.table_name;
"""


async def main():
    conn = await asyncpg.connect(DB_URL, timeout=30)

    print("=== ALL public tables with free-text customer columns ===")
    rows = await conn.fetch(SQL_INSCOPE)
    for r in rows:
        print(f"  {r['table_name']:38s}  text_cols={list(r['text_cols'])}  has_customer_id={r['has_customer_id']}")

    print("\n=== IN-SCOPE (new build) tables with free-text customer references and no FK ===")
    rows2 = await conn.fetch(SQL_INSCOPE_NEW)
    for r in rows2:
        print(f"  {r['table_name']:38s}  text_cols={list(r['text_customer_cols'])}  "
              f"has_id_col={r['has_customer_id_column']}  has_fk_to_customer_master={r['customer_id_fk_present']}")

    print("\n=== FKs targeting customer_master / mst_customer / customers ===")
    rows3 = await conn.fetch(SQL_FK_TO_CUSTOMER_MASTER)
    if not rows3:
        print("  (none)")
    for r in rows3:
        print(f"  {r['from_table']}.{r['from_column']} -> {r['to_table']}.{r['to_column']}  ({r['constraint_name']})")

    print("\n=== po_line distinct status values ===")
    rows4 = await conn.fetch(SQL_PO_LINE_DISTINCT_STATUS)
    if not rows4:
        print("  (table empty)")
    for r in rows4:
        print(f"  {r['status']}")

    print("\n=== po_line CHECK constraints ===")
    rows5 = await conn.fetch(SQL_PO_LINE_STATUS_CHK)
    for r in rows5:
        print(f"  {r['def']}")

    print("\n=== FKs targeting vendor_master ===")
    rows6 = await conn.fetch(SQL_SUPPLIER_FKS)
    for r in rows6:
        print(f"  {r['table_name']}.{r['column_name']} -> {r['ref_table']}.{r['ref_column']}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
