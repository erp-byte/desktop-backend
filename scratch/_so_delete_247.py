"""Delete ONLY CF-SO/26-27/247 (so_id=7991) — single SO explicitly named by user."""
import asyncio, os
from pathlib import Path
import asyncpg

for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

TARGET_SO_NUMBER = "CF-SO/26-27/247"

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            so_id = await conn.fetchval(
                "SELECT so_id FROM so_header WHERE so_number = $1", TARGET_SO_NUMBER
            )
            if so_id is None:
                print(f"{TARGET_SO_NUMBER} not found in so_header — nothing to do.")
                return
            print(f"Target: {TARGET_SO_NUMBER} -> so_id={so_id}")

            f_ids = [r["fulfillment_id"] for r in await conn.fetch(
                "SELECT fulfillment_id FROM so_fulfillment WHERE so_id = $1", so_id
            )]

            checks = [
                ("active fulfillments",
                 "SELECT COUNT(*) FROM so_fulfillment WHERE so_id = $1 "
                 "AND (produced_qty_kg > 0 OR dispatched_qty_kg > 0 OR order_status <> 'open')",
                 (so_id,)),
                ("inventory_batch.blocked_for_so_id",
                 "SELECT COUNT(*) FROM inventory_batch WHERE blocked_for_so_id = $1", (so_id,)),
                ("inventory_event_log",
                 "SELECT COUNT(*) FROM inventory_event_log WHERE so_id = $1", (so_id,)),
                ("batch_block_history",
                 "SELECT COUNT(*) FROM batch_block_history WHERE so_id = $1", (so_id,)),
                ("batch_rejection_log",
                 "SELECT COUNT(*) FROM batch_rejection_log WHERE so_id = $1", (so_id,)),
                ("cascade_events",
                 "SELECT COUNT(*) FROM cascade_events WHERE old_so_id = $1 OR new_so_id = $1", (so_id,)),
                ("log_edit (so_header)",
                 "SELECT COUNT(*) FROM log_edit WHERE table_name='so_header' AND record_id = $1", (so_id,)),
            ]
            if f_ids:
                checks += [
                    ("so_revision_log",
                     "SELECT COUNT(*) FROM so_revision_log WHERE fulfillment_id = ANY($1::int[])", (f_ids,)),
                    ("fulfillment_floor_stock",
                     "SELECT COUNT(*) FROM fulfillment_floor_stock WHERE fulfillment_id = ANY($1::int[])", (f_ids,)),
                    ("fulfillment_bom_override",
                     "SELECT COUNT(*) FROM fulfillment_bom_override WHERE fulfillment_id = ANY($1::int[])", (f_ids,)),
                    ("carryforward children",
                     "SELECT COUNT(*) FROM so_fulfillment WHERE carryforward_from_id = ANY($1::int[])", (f_ids,)),
                ]

            blockers = {}
            for label, sql, args in checks:
                n = await conn.fetchval(sql, *args)
                if n:
                    blockers[label] = n
            if blockers:
                print("ABORT — downstream refs:")
                for k, v in blockers.items():
                    print(f"  {k}: {v}")
                raise RuntimeError("blocked")

            n_gst  = (await conn.execute("DELETE FROM so_gst_reconciliation WHERE so_id = $1", so_id)).split()[-1]
            n_full = (await conn.execute("DELETE FROM so_fulfillment        WHERE so_id = $1", so_id)).split()[-1]
            n_line = (await conn.execute("DELETE FROM so_line               WHERE so_id = $1", so_id)).split()[-1]
            n_head = (await conn.execute("DELETE FROM so_header             WHERE so_id = $1", so_id)).split()[-1]
            print(f"so_gst_reconciliation deleted: {n_gst}")
            print(f"so_fulfillment        deleted: {n_full}")
            print(f"so_line               deleted: {n_line}")
            print(f"so_header             deleted: {n_head}")
        # transaction auto-commits on context exit
        print("COMMITTED.")

        # verify
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM so_header WHERE so_number = $1", TARGET_SO_NUMBER
        )
        print(f"Post-commit so_header rows for {TARGET_SO_NUMBER}: {remaining}")
    finally:
        await conn.close()

asyncio.run(main())
