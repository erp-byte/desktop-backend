import asyncio, os, asyncpg
from dotenv import load_dotenv
from app.modules.transfer.services import query_service as q
load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    for st in (None, "Pending", "Transferred"):
        r = await q.list_requests(c, page=1, per_page=500, status=st)
        print(f"  status={st!r:14} total={r['total']}")
    await c.close()


asyncio.run(main())
