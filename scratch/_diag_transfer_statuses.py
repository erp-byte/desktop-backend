import asyncio, os, asyncpg
from dotenv import load_dotenv
load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    print("interunit_transfers_header status:")
    for r in await c.fetch(
        "SELECT status, COUNT(*) n FROM interunit_transfers_header GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']!r:16} {r['n']}")
    print("\ninterunit_transfer_in_header status:")
    for r in await c.fetch(
        "SELECT status, COUNT(*) n FROM interunit_transfer_in_header GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']!r:16} {r['n']}")
    await c.close()


asyncio.run(main())
