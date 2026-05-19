"""List SO/fulfillment tables with column counts and row counts."""
import asyncio, os
from dotenv import load_dotenv
load_dotenv()
import asyncpg


async def run():
    c = await asyncpg.connect(os.environ['DATABASE_URL'])
    rows = await c.fetch("""
        SELECT t.table_name,
               (SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name=t.table_name AND table_schema='public') AS col_count
        FROM information_schema.tables t
        WHERE table_schema='public'
          AND (t.table_name LIKE 'so_%'
               OR t.table_name LIKE '%fulfill%'
               OR t.table_name LIKE '%sales%'
               OR t.table_name LIKE 'customer%'
               OR t.table_name LIKE '%invoice%'
               OR t.table_name LIKE '%dispatch%')
        ORDER BY t.table_name
    """)
    print(f'{"table":<40} {"cols":>5} {"rows":>10}')
    print('-' * 60)
    for r in rows:
        tbl = r['table_name']
        try:
            row_count = await c.fetchval(f'SELECT COUNT(*) FROM "{tbl}"')
        except Exception as e:
            row_count = f'ERR: {type(e).__name__}'
        print(f'{tbl:<40} {r["col_count"]:>5} {row_count:>10}')
    await c.close()


asyncio.run(run())
