"""Reproduce the 500 on /orders/create-from-plan for plan 58.

Calls the engine function directly so the traceback lands here instead of
the uvicorn console. No HTTP layer in the way.
"""
import asyncio
import asyncpg
import os
import traceback

from app.config import Settings

settings = Settings()


async def main():
    pool = await asyncpg.create_pool(settings.DATABASE_URL, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            plan = await conn.fetchrow(
                "SELECT plan_id, status, entity FROM production_plan WHERE plan_id = $1",
                58,
            )
            print("plan row:", dict(plan) if plan else None)

            from app.modules.production.services.job_card_engine import create_production_orders

            async with conn.transaction():
                try:
                    result = await create_production_orders(conn, 58, plan["entity"])
                    print("result:", result)
                    # Roll back so we don't double-create if engine actually succeeds
                    raise RuntimeError("PROBE_ROLLBACK_MARKER")
                except RuntimeError as e:
                    if "PROBE_ROLLBACK_MARKER" in str(e):
                        print("rolled back probe transaction")
                    else:
                        raise
    except Exception:
        print("--- TRACEBACK ---")
        traceback.print_exc()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
