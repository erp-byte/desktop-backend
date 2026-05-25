"""Extra one-off checks for views and entity columns relevant to closing-stock."""
import asyncio, os
from dotenv import load_dotenv
import asyncpg
load_dotenv()
DB_URL = os.environ["DATABASE_URL"]

EXTRA = [
    ("v_billed_articles_cnt", "SELECT count(*) FROM v_billed_articles"),
    ("v_billed_details_cnt", "SELECT count(*) FROM v_billed_details"),
    ("v_billed_articles_cols", """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name='v_billed_articles' ORDER BY ordinal_position"""),
    ("v_billed_details_cols", """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name='v_billed_details' ORDER BY ordinal_position"""),
    # Sample window check on cfpl_transactions_v2 by entity differentiation
    ("cfpl_v2_window_summary", """
        SELECT count(*) AS n,
               min(entry_date) AS mn, max(entry_date) AS mx,
               count(*) FILTER (WHERE rtv=true) AS rtv_rows,
               count(*) FILTER (WHERE service=true) AS svc_rows
        FROM cfpl_transactions_v2
        WHERE entry_date::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    ("cdpl_v2_window_summary", """
        SELECT count(*) AS n,
               min(entry_date) AS mn, max(entry_date) AS mx,
               count(*) FILTER (WHERE rtv=true) AS rtv_rows
        FROM cdpl_transactions_v2
        WHERE entry_date::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    # is interunit header tagged with entity?
    ("iut_header_sites", """
        SELECT from_site, to_site, count(*) FROM interunit_transfers_header
        WHERE stock_trf_date BETWEEN '2026-04-01' AND '2026-05-18'
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20"""),
    # Inter unit transfer in header — has receiving warehouse only
    ("iut_in_warehouses", """
        SELECT receiving_warehouse, count(*)
        FROM interunit_transfer_in_header
        WHERE received_at::date BETWEEN '2026-04-01' AND '2026-05-18'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""),
    # Sample CDPL articles/boxes sku_name extraction
    ("cdpl_articles_window", """
        SELECT count(*), sum(po_quantity), sum(po_weight)
        FROM cdpl_articles_v2
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    ("cfpl_articles_window", """
        SELECT count(*), sum(po_quantity), sum(po_weight)
        FROM cfpl_articles_v2
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    # Does cfpl_transactions_v2.warehouse distinguish entity?
    ("cfpl_v2_warehouses", """
        SELECT warehouse, count(*) FROM cfpl_transactions_v2
        WHERE entry_date::date BETWEEN '2026-04-01' AND '2026-05-18'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 30"""),
    # qty totals from cfpl/cdpl boxes
    ("cfpl_boxes_window_total", """
        SELECT count(*) AS n, sum(net_weight) AS sum_net, sum(gross_weight) AS sum_gross
        FROM cfpl_boxes_v2
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    ("cdpl_boxes_window_total", """
        SELECT count(*) AS n, sum(net_weight) AS sum_net, sum(gross_weight) AS sum_gross
        FROM cdpl_boxes_v2
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    # iut lines window total
    ("iut_lines_window_total", """
        SELECT count(*) AS n, sum(net_weight) AS sum_net, sum(total_weight) AS sum_tot
        FROM interunit_transfers_lines
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    # rtv totals
    ("cfpl_rtv_lines_window_total", """
        SELECT count(*) AS n, sum(net_weight) AS sum_net, sum(qty) AS sum_qty
        FROM cfpl_rtv_lines
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    ("cdpl_rtv_lines_window_total", """
        SELECT count(*) AS n, sum(net_weight) AS sum_net, sum(qty) AS sum_qty
        FROM cdpl_rtv_lines
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    # job card consumption check
    ("jc_rm_indent_issued_window", """
        SELECT count(*) AS rows, sum(issued_qty) AS issued, sum(reqd_qty) AS reqd
        FROM job_card_rm_indent
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    ("jc_pm_indent_issued_window", """
        SELECT count(*) AS rows, sum(issued_qty) AS issued
        FROM job_card_pm_indent
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    # Material consumption only has 1 row — check if jc_balance/jc_output are richer
    ("jc_output_window", """
        SELECT count(*) rows, sum(rm_consumed_kg) consumed, sum(fg_actual_kg) fg
        FROM job_card_output
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
    # Check job_card entity tagging
    ("job_card_entity_split", """
        SELECT entity, factory, count(*) FROM job_card
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'
        GROUP BY 1,2 ORDER BY 3 DESC"""),
    # Outward — only cfpl_outward has 1 stale row. Are there alternative outward sources?
    ("any_outward_window", """
        SELECT 'cfpl_outward' AS t, count(*)
        FROM cfpl_outward WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'
        UNION ALL SELECT 'cdpl_outward', count(*) FROM cdpl_outward
        WHERE created_at::date BETWEEN '2026-04-01' AND '2026-05-18'
        UNION ALL SELECT 'sales_outward', count(*) FROM sales_outward
        WHERE dispatched_at::date BETWEEN '2026-04-01' AND '2026-05-18'"""),
]


async def main():
    conn = await asyncpg.connect(DB_URL, timeout=30)
    for name, sql in EXTRA:
        print("=" * 60)
        print(name)
        try:
            rows = await conn.fetch(sql)
            for r in rows:
                print("  ", dict(r))
        except Exception as e:
            print("  ERROR:", e)
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
