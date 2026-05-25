"""Run the SO deletion in a single transaction with the same safety guard."""
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
        async with conn.transaction():
            so_ids = [r["so_id"] for r in await conn.fetch(
                "SELECT so_id FROM so_header WHERE so_number = ANY($1::text[])", SOS
            )]
            print(f"so_ids to delete: {len(so_ids)}")
            if not so_ids:
                print("Nothing to delete.")
                return

            f_ids = [r["fulfillment_id"] for r in await conn.fetch(
                "SELECT fulfillment_id FROM so_fulfillment WHERE so_id = ANY($1::int[])", so_ids
            )]

            # Safety guard — abort if any production-side data exists
            checks = {
                "active fulfillments": (
                    "SELECT COUNT(*) FROM so_fulfillment WHERE so_id = ANY($1::int[]) "
                    "AND (produced_qty_kg > 0 OR dispatched_qty_kg > 0 OR order_status <> 'open')",
                    so_ids,
                ),
                "inventory_batch.blocked_for_so_id": (
                    "SELECT COUNT(*) FROM inventory_batch WHERE blocked_for_so_id = ANY($1::int[])",
                    so_ids,
                ),
                "inventory_event_log": (
                    "SELECT COUNT(*) FROM inventory_event_log WHERE so_id = ANY($1::int[])",
                    so_ids,
                ),
                "batch_block_history": (
                    "SELECT COUNT(*) FROM batch_block_history WHERE so_id = ANY($1::int[])",
                    so_ids,
                ),
                "batch_rejection_log": (
                    "SELECT COUNT(*) FROM batch_rejection_log WHERE so_id = ANY($1::int[])",
                    so_ids,
                ),
                "cascade_events": (
                    "SELECT COUNT(*) FROM cascade_events WHERE old_so_id = ANY($1::int[]) OR new_so_id = ANY($1::int[])",
                    so_ids,
                ),
                "so_revision_log": (
                    "SELECT COUNT(*) FROM so_revision_log WHERE fulfillment_id = ANY($1::int[])",
                    f_ids,
                ),
                "fulfillment_floor_stock": (
                    "SELECT COUNT(*) FROM fulfillment_floor_stock WHERE fulfillment_id = ANY($1::int[])",
                    f_ids,
                ),
                "fulfillment_bom_override": (
                    "SELECT COUNT(*) FROM fulfillment_bom_override WHERE fulfillment_id = ANY($1::int[])",
                    f_ids,
                ),
                "carryforward children": (
                    "SELECT COUNT(*) FROM so_fulfillment WHERE carryforward_from_id = ANY($1::int[])",
                    f_ids,
                ),
                "log_edit (so_header)": (
                    "SELECT COUNT(*) FROM log_edit WHERE table_name='so_header' AND record_id = ANY($1::int[])",
                    so_ids,
                ),
            }
            blockers = {}
            for label, (sql, ids) in checks.items():
                n = await conn.fetchval(sql, ids) if ids else 0
                if n:
                    blockers[label] = n
            if blockers:
                print("ABORT — downstream refs present:")
                for k, v in blockers.items():
                    print(f"  {k}: {v}")
                raise RuntimeError("blocked")

            n_gst  = await conn.fetchval("DELETE FROM so_gst_reconciliation WHERE so_id = ANY($1::int[]) RETURNING 1", so_ids)
            # asyncpg fetchval returns first row's first col; need execute for status:
            n_gst   = (await conn.execute("DELETE FROM so_gst_reconciliation WHERE so_id = ANY($1::int[])", so_ids)).split()[-1]
            n_full  = (await conn.execute("DELETE FROM so_fulfillment        WHERE so_id = ANY($1::int[])", so_ids)).split()[-1]
            n_line  = (await conn.execute("DELETE FROM so_line               WHERE so_id = ANY($1::int[])", so_ids)).split()[-1]
            n_head  = (await conn.execute("DELETE FROM so_header             WHERE so_id = ANY($1::int[])", so_ids)).split()[-1]
            print(f"Deleted so_gst_reconciliation: {n_gst}")
            print(f"Deleted so_fulfillment:        {n_full}")
            print(f"Deleted so_line:               {n_line}")
            print(f"Deleted so_header:             {n_head}")
    finally:
        await conn.close()

asyncio.run(main())
