"""Read-only probe for job-card seed task: SO CF-SO/26-27/130, PO 896.

Checks:
  - so_fulfillment row for the SO
  - production_order row for PO '896' or 'PRD-2026-0896' (any variant)
  - bom_header for sliced cranberries 100g
  - job_card extended columns (article_code/mrp/ean/sales_order_ref/fumigation/etc.)
  - prod_order_number / batch_number uniqueness collision

Writes results to stdout.
"""
import asyncio
import asyncpg

DB_URL = "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db"


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        print("=" * 70)
        print("1) so_fulfillment for CF-SO/26-27/130")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT f.fulfillment_id, h.so_number, f.customer_name, f.fg_sku_name, "
            "       f.original_qty_kg, f.pending_qty_kg, f.produced_qty_kg, "
            "       f.order_status, f.entity, f.financial_year, f.so_id, f.so_line_id "
            "  FROM so_fulfillment f "
            "  JOIN so_line l ON f.so_line_id = l.so_line_id "
            "  JOIN so_header h ON l.so_id = h.so_id "
            " WHERE h.so_number = $1",
            "CF-SO/26-27/130",
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("  (none)")

        print("\n" + "=" * 70)
        print("2) production_order matching '896' / 'PRD-*-896' / 'PO*896'")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT prod_order_id, prod_order_number, plan_line_id, bom_id, "
            "       fg_sku_name, customer_name, batch_number, batch_size_kg, "
            "       net_wt_per_unit, status, entity "
            "  FROM production_order "
            " WHERE prod_order_number IN ('896','PRD-2026-0896','PRD-2026-896','PO-896') "
            "    OR prod_order_number ILIKE '%896' "
            " ORDER BY prod_order_id DESC LIMIT 10",
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("  (none)")

        print("\n" + "=" * 70)
        print("3) batch_number JD27 already used?")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT prod_order_id, prod_order_number, batch_number, fg_sku_name "
            "  FROM production_order WHERE batch_number = $1",
            "JD27",
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("  (none — safe to use JD27)")

        print("\n" + "=" * 70)
        print("4) bom_header for cranberry sliced 100g")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT bom_id, fg_sku_name, customer_code, business_unit, factory, "
            "       floors, pack_size_kg, shelf_life_days "
            "  FROM bom_header "
            " WHERE fg_sku_name ILIKE '%cranberr%' "
            " ORDER BY bom_id LIMIT 20",
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("  (none)")

        print("\n" + "=" * 70)
        print("5) job_card columns (looking for article_code/mrp/ean/sales_order_ref/")
        print("   fumigation/control_sample_gm/metal_detector_used/etc.)")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "  FROM information_schema.columns "
            " WHERE table_name = 'job_card' "
            "   AND column_name IN ("
            "     'article_code','mrp','ean','sales_order_ref','fumigation',"
            "     'metal_detector_used','roasting_pasteurization','magnets_used',"
            "     'control_sample_gm','bu','store_allocation_status'"
            "   ) ORDER BY column_name",
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("  (none — these extras don't exist; we'll skip them on insert)")

        print("\n" + "=" * 70)
        print("6) production_order columns sanity (machine_id, customer_name)")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "  FROM information_schema.columns "
            " WHERE table_name = 'production_order' "
            "   AND column_name IN ('machine_id','customer_name','floor','factory') "
            " ORDER BY column_name",
        )
        for r in rows:
            print(dict(r))

        print("\n" + "=" * 70)
        print("7) job_card_output columns sanity")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT column_name, data_type "
            "  FROM information_schema.columns "
            " WHERE table_name = 'job_card_output' "
            " ORDER BY ordinal_position",
        )
        for r in rows:
            print(f"  {r['column_name']:30s} {r['data_type']}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
