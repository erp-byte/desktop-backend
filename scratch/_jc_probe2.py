"""Round-2 probe: exact BOM for PL SLICED CRANBERRIES 100G, prod_order_number convention."""
import asyncio
import asyncpg

DB_URL = "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db"


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        print("=" * 70)
        print("A) BOM exact match: PL SLICED CRANBERRIES 100G")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT bom_id, fg_sku_name, customer_code, business_unit, factory, "
            "       floors, pack_size_kg, shelf_life_days "
            "  FROM bom_header "
            " WHERE fg_sku_name ILIKE '%PL%SLICED%CRANBERR%' "
            "    OR fg_sku_name ILIKE '%sliced cranberr%100g%' "
            " ORDER BY bom_id",
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("  (none)")

        print("\n" + "=" * 70)
        print("B) BOM bom_line for that BOM (if found)")
        print("=" * 70)
        if rows:
            bom_id = rows[0]['bom_id']
            lines = await conn.fetch(
                "SELECT material_sku_name, item_type, uom, quantity_per_unit, loss_pct, godown "
                "  FROM bom_line WHERE bom_id = $1 ORDER BY item_type, material_sku_name",
                bom_id,
            )
            for l in lines:
                print(dict(l))

        print("\n" + "=" * 70)
        print("C) prod_order_number convention — last 10 created")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT prod_order_number, batch_number, fg_sku_name, status, created_at "
            "  FROM production_order ORDER BY prod_order_id DESC LIMIT 10",
        )
        for r in rows:
            print(dict(r))
        if not rows:
            print("  (no production_order rows yet)")

        print("\n" + "=" * 70)
        print("D) job_card columns full list (so we know all NOT NULLs)")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default "
            "  FROM information_schema.columns WHERE table_name = 'job_card' "
            " ORDER BY ordinal_position",
        )
        for r in rows:
            print(f"  {r['column_name']:32s} {r['data_type']:25s} null={r['is_nullable']:3s} default={r['column_default']}")

        print("\n" + "=" * 70)
        print("E) production_order columns full list")
        print("=" * 70)
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, column_default "
            "  FROM information_schema.columns WHERE table_name = 'production_order' "
            " ORDER BY ordinal_position",
        )
        for r in rows:
            print(f"  {r['column_name']:32s} {r['data_type']:25s} null={r['is_nullable']:3s} default={r['column_default']}")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
