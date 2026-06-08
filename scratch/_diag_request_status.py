import asyncio, os, asyncpg
from dotenv import load_dotenv
load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    print("interunit_transfer_requests distinct status (count):")
    for r in await c.fetch(
        "SELECT status, COUNT(*) n FROM interunit_transfer_requests GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']!r:20} {r['n']}")
    print("\nauth_user.allowed_warehouses normalization preview:")
    for code in ("A-185", "W-202"):
        n = await c.fetchval(
            "SELECT COUNT(*) FROM auth_user WHERE $1 = ANY(allowed_warehouses)", code)
        print(f"  rows containing {code!r}: {n}")
    await c.close()


asyncio.run(main())
