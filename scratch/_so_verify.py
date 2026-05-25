import asyncio, os
from pathlib import Path
import asyncpg

for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

SOS = [f"CF-SO/26-27/{n}" for n in range(1, 250) if n not in (199, 231)]

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(
            "SELECT so_number FROM so_header WHERE so_number = ANY($1::text[]) ORDER BY so_number",
            SOS,
        )
        print(f"Still present in so_header: {len(rows)} / {len(SOS)}")
        if rows:
            for r in rows[:20]:
                print(f"  {r['so_number']}")
            if len(rows) > 20:
                print(f"  ... and {len(rows)-20} more")
        # also count residual lines/fulfillments by so_id if any so_ids remain
        n_line = await conn.fetchval(
            "SELECT COUNT(*) FROM so_line l JOIN so_header h USING(so_id) WHERE h.so_number = ANY($1::text[])",
            SOS,
        )
        n_full = await conn.fetchval(
            "SELECT COUNT(*) FROM so_fulfillment f JOIN so_header h USING(so_id) WHERE h.so_number = ANY($1::text[])",
            SOS,
        )
        n_gst = await conn.fetchval(
            "SELECT COUNT(*) FROM so_gst_reconciliation g JOIN so_header h USING(so_id) WHERE h.so_number = ANY($1::text[])",
            SOS,
        )
        print(f"Residual so_line rows: {n_line}")
        print(f"Residual so_fulfillment rows: {n_full}")
        print(f"Residual so_gst_reconciliation rows: {n_gst}")
    finally:
        await conn.close()

asyncio.run(main())
