"""One-off: record the QC arrival for a PO transaction via the real
qc_intimation.record_arrivals service (same path the Purchase 'Send Intimation'
button uses). Persists pending qc_intimation rows so the QC Start Inspection
picker can find them. Idempotency note: record_arrivals does NOT dedup — run once."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

from app.modules.purchase.services import qc_intimation

load_dotenv()

TXN = "TR-20260429121623-7BD3"


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM qc_intimation WHERE transaction_no = $1", TXN)
        if existing:
            print(f"Already {existing} qc_intimation rows for {TXN}; skipping to avoid duplicates.")
            await pool.close()
            return
        header = await conn.fetchrow(
            """SELECT po_number, supplier_id, vendor_supplier_name, entity,
                      vehicle_number, invoice_number
               FROM po_header WHERE transaction_no = $1""", TXN)
        if header is None:
            print(f"No po_header for {TXN}")
            await pool.close()
            return
        lines = [dict(r) for r in await conn.fetch(
            "SELECT * FROM po_line WHERE transaction_no = $1 ORDER BY line_number", TXN)]

    n = await qc_intimation.record_arrivals(
        pool,
        po_number=header["po_number"],
        transaction_no=TXN,
        supplier_id=header["supplier_id"],
        supplier_name=header["vendor_supplier_name"],
        entity=header["entity"],
        vehicle_no=header["vehicle_number"],
        invoice_no=header["invoice_number"],
        lines=lines,
    )
    print(f"Inserted {n} pending qc_intimation rows for {TXN}")

    # verify via the same service the picker uses
    from app.modules.qc.services import inspection_service as insp
    pending = await insp.list_pending_intimations(pool, q=TXN, limit=50)
    print(f"Picker now returns {len(pending)} pending arrival(s) for that txn:")
    for p in pending:
        print(f"  - id={p['qc_intimation_id']} sku={p['sku_name']!r} lot={p['lot_number']}")
    await pool.close()


asyncio.run(main())
