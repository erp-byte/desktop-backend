import asyncio, os, asyncpg
from dotenv import load_dotenv
load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    print("In-Transit destination_table distribution:")
    for r in await c.fetch(
        "SELECT destination_table, COUNT(*) n, COUNT(DISTINCT transfer_out_id) tos "
        "FROM pending_transfer_stock WHERE status='In Transit' GROUP BY destination_table ORDER BY n DESC"):
        print(f"  {r['destination_table']!r:28} rows={r['n']:5} transfer_outs={r['tos']}")

    print("\nCold-dest in-transit transfer_outs WITHOUT a transfer-in:")
    rows = await c.fetch(
        """
        SELECT pts.transfer_out_id tid, pts.destination_table dest, COUNT(*) n
        FROM pending_transfer_stock pts
        WHERE pts.status='In Transit' AND pts.destination_table LIKE '%cold_stocks'
          AND NOT EXISTS (SELECT 1 FROM interunit_transfer_in_header t WHERE t.transfer_out_id=pts.transfer_out_id)
        GROUP BY pts.transfer_out_id, pts.destination_table ORDER BY n ASC LIMIT 5
        """)
    print(f"  found {len(rows)}")
    for r in rows:
        print(f"  tid={r['tid']} dest={r['dest']} n={r['n']}")

    print("\nAny cold-dest in-transit transfer_out (ignoring transfer-in):")
    rows = await c.fetch(
        "SELECT transfer_out_id tid, destination_table dest, COUNT(*) n FROM pending_transfer_stock "
        "WHERE status='In Transit' AND destination_table LIKE '%cold_stocks' "
        "GROUP BY transfer_out_id, destination_table ORDER BY n ASC LIMIT 5")
    for r in rows:
        has_ti = await c.fetchval("SELECT status FROM interunit_transfer_in_header WHERE transfer_out_id=$1 LIMIT 1", r["tid"])
        print(f"  tid={r['tid']} dest={r['dest']} n={r['n']} existing_transfer_in={has_ti}")
    await c.close()


asyncio.run(main())
