"""Read-only probe for production plan #55.

Checks:
  - production_plan row (status, entity)
  - production_plan_line rows (bom_id, status, fg_sku_name, planned_qty_kg)
  - bom_header existence for each line's bom_id
  - any existing production_order already created against these plan lines
"""
import asyncio
import asyncpg

DB_URL = "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db"

PLAN_ID = 55


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        print("=" * 70)
        print(f"1) production_plan {PLAN_ID}")
        print("=" * 70)
        plan = await conn.fetchrow(
            "SELECT plan_id, status, entity, approved_at, approved_by, created_at "
            "FROM production_plan WHERE plan_id = $1",
            PLAN_ID,
        )
        if not plan:
            print("  (no such plan)")
            return
        print(dict(plan))

        print("\n" + "=" * 70)
        print(f"2) production_plan_line for plan {PLAN_ID}")
        print("=" * 70)
        lines = await conn.fetch(
            "SELECT plan_line_id, bom_id, fg_sku_name, customer_name, "
            "       planned_qty_kg, status, floor, machine_id "
            "FROM production_plan_line WHERE plan_id = $1 ORDER BY plan_line_id",
            PLAN_ID,
        )
        for ln in lines:
            print(dict(ln))
        if not lines:
            print("  (no lines)")

        print("\n" + "=" * 70)
        print("3) bom_header lookup for each line's bom_id")
        print("=" * 70)
        for ln in lines:
            bom_id = ln['bom_id']
            if bom_id is None:
                print(f"  plan_line {ln['plan_line_id']}: bom_id=NULL  ← will be SKIPPED by create_production_orders")
                continue
            bom = await conn.fetchrow(
                "SELECT bom_id, fg_sku_name, customer_name, pack_size_kg, factory, floors "
                "FROM bom_header WHERE bom_id = $1",
                bom_id,
            )
            if not bom:
                print(f"  plan_line {ln['plan_line_id']}: bom_id={bom_id} → bom_header NOT FOUND  ← will be SKIPPED")
            else:
                print(f"  plan_line {ln['plan_line_id']}: bom_id={bom_id} → OK ({dict(bom)})")

        print("\n" + "=" * 70)
        print("4) Existing production_order for these plan_line_ids")
        print("=" * 70)
        if lines:
            ids = [ln['plan_line_id'] for ln in lines]
            existing = await conn.fetch(
                "SELECT prod_order_id, prod_order_number, plan_line_id, status, created_at "
                "FROM production_order WHERE plan_line_id = ANY($1::int[]) "
                "ORDER BY created_at DESC",
                ids,
            )
            for r in existing:
                print(dict(r))
            if not existing:
                print("  (none — no orders created yet against any line of plan 55)")

        print("\n" + "=" * 70)
        print("5) Summary — what create_production_orders(plan_id=55) would do")
        print("=" * 70)
        if plan['status'] != 'approved':
            print(f"  BLOCKER: plan status is {plan['status']!r}, must be 'approved'")
        eligible = [ln for ln in lines if ln['status'] == 'planned' and ln['bom_id']]
        print(f"  lines total              : {len(lines)}")
        print(f"  lines with status=planned: {sum(1 for ln in lines if ln['status'] == 'planned')}")
        print(f"  lines with bom_id NULL   : {sum(1 for ln in lines if ln['bom_id'] is None)}")
        print(f"  lines eligible           : {len(eligible)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
