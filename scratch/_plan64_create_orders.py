"""Create production orders for plan 64 directly via the engine.

Matches what POST /orders/create-from-plan does (router wraps engine in
a transaction). MAX(suffix)+1 currently returns 37, so this should
insert PRD-2026-0037 cleanly without colliding with the existing
PRD-2026-0036.
"""
import asyncio
import asyncpg

from app.config import Settings

settings = Settings()
PLAN_ID = 64


async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            plan = await conn.fetchrow(
                "SELECT plan_id, status, entity FROM production_plan WHERE plan_id = $1",
                PLAN_ID,
            )
            if not plan:
                print(f"plan {PLAN_ID} not found")
                return
            print(f"plan {PLAN_ID}: status={plan['status']} entity={plan['entity']}")

            # Confirm no orders yet for this plan
            existing = await conn.fetch(
                """
                SELECT po.prod_order_id, po.prod_order_number, po.plan_line_id
                  FROM production_order po
                  JOIN production_plan_line pl ON pl.plan_line_id = po.plan_line_id
                 WHERE pl.plan_id = $1
                """,
                PLAN_ID,
            )
            if existing:
                print(f"Plan {PLAN_ID} already has {len(existing)} orders:")
                for o in existing:
                    print(f"  {dict(o)}")
                print("Aborting — already has orders, refusing to duplicate.")
                return
            print(f"plan {PLAN_ID} has no orders — safe to create")

            from app.modules.production.services.job_card_engine import create_production_orders

            async with conn.transaction():
                result = await create_production_orders(conn, PLAN_ID, plan["entity"])
                print(f"result: {result}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
