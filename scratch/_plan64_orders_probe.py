"""Inspect plan 64 and its production_orders.

Goal: understand which plan_lines have duplicate production_orders so we can
decide whether to delete duplicates or let MAX+1 (which is now 37) advance.
"""
import asyncio
import asyncpg

from app.config import Settings

settings = Settings()


async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            for plan_id in (63, 64):
                plan = await conn.fetchrow(
                    "SELECT plan_id, status, entity, created_at FROM production_plan WHERE plan_id = $1",
                    plan_id,
                )
                print(f"\n=== plan {plan_id} ===")
                print(f"  {dict(plan) if plan else 'NOT FOUND'}")

                lines = await conn.fetch(
                    "SELECT plan_line_id, fg_sku_name, status, planned_qty_kg "
                    "FROM production_plan_line WHERE plan_id = $1 ORDER BY plan_line_id",
                    plan_id,
                )
                print(f"  {len(lines)} plan_lines:")
                for pl in lines:
                    print(f"    plan_line={pl['plan_line_id']} sku={pl['fg_sku_name']} "
                          f"status={pl['status']} qty={pl['planned_qty_kg']}")

                    orders = await conn.fetch(
                        "SELECT prod_order_id, prod_order_number, status, created_at "
                        "FROM production_order WHERE plan_line_id = $1 ORDER BY prod_order_id",
                        pl['plan_line_id'],
                    )
                    for o in orders:
                        print(f"      -> order id={o['prod_order_id']} num={o['prod_order_number']} "
                              f"status={o['status']} created={o['created_at']}")

            # Check existence of has-already-created-orders pattern check
            print("\n=== Recent production_orders (last 20) ===")
            recent = await conn.fetch(
                "SELECT prod_order_id, prod_order_number, plan_line_id, status, created_at "
                "FROM production_order ORDER BY prod_order_id DESC LIMIT 20",
            )
            for o in recent:
                print(f"  id={o['prod_order_id']} num={o['prod_order_number']} "
                      f"plan_line={o['plan_line_id']} status={o['status']} created={o['created_at']}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
