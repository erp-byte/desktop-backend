"""NON-DESTRUCTIVE end-to-end check for create_service.create_transfer.

Runs the full create against the real DB inside an OUTER transaction that is
ALWAYS rolled back — so header/lines/boxes inserts and the pending park are all
reverted. Nothing is committed.

This DB holds the interunit_* transfer tables but NOT the source-inventory tables
(boxes_v2 / bulk_entry / cold_stocks live in the separate warehouse_db). So the
test uses a SYNTHETIC box: it exercises the real interunit_transfers_* writes +
the pending park's graceful no-source branch (each source table is guarded by
_table_exists). Real deduction is the same verified code backfill uses and only
fires where the source tables exist (production).

Run:  PYTHONPATH=. .venv/bin/python tests/services/test_create_transfer_rollback.py
"""
import asyncio

import asyncpg
from fastapi import HTTPException

from app.config import Settings
from app.modules.transfer import schemas
from app.modules.transfer.services import create_service

REMARK = "ROLLBACK TEST — not committed"


async def main() -> None:
    s = Settings()
    conn = await asyncpg.connect(s.DATABASE_URL, timeout=10)
    print("connected.")
    # The interunit_*/pending_transfer_stock tables are externally-managed (they live in
    # the production warehouse_db). Dev/CI DBs carry only minimal STUB columns
    # (see app/db/test_db/db/000_external_stub_tables.sql), so the full create path can't
    # run there. Skip cleanly unless the full transfer schema is present.
    cols = {r["column_name"] for r in await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='interunit_transfers_header'")}
    required = {"stock_trf_date", "vehicle_no", "remark", "reason_code", "created_by", "created_ts"}
    missing = required - cols
    if missing:
        print(f"SKIP: stub transfer schema (missing header cols: {sorted(missing)}). "
              "Run against a DB with the full production transfer schema.")
        await conn.close()
        return
    tx = conn.transaction()
    await tx.start()
    try:
        synthetic_box_id = "ROLLBACKTEST-BOX-1"
        synthetic_tno = "BE-ROLLBACKTEST"

        payload = schemas.TransferCreate(
            header=schemas.TransferHeaderCreate(
                stock_trf_date="23-06-2026", from_warehouse="A68", to_warehouse="W202",
                vehicle_no="MH43BP6885", reason_code="Stock Requirement", remark=REMARK,
            ),
            lines=[schemas.TransferLineCreate(
                material_type="RM", item_category="TEST", sub_category="TEST",
                item_description="ROLLBACK TEST ITEM", quantity="1", pack_size="25",
                lot_number="LOT-RBT",
            )],
            boxes=[schemas.BoxCreate(
                box_number=1, box_id=synthetic_box_id, article="ROLLBACK TEST ITEM",
                lot_number="LOT-RBT", transaction_no=synthetic_tno,
                net_weight="25.0", gross_weight="25.5",
            )],
            request_id=None,
        )

        result = await create_service.create_transfer(conn, payload, "rollback-test@candorfoods.in")
        hid = result["id"]
        print(f"created header id={hid} challan={result['challan_no']} status={result['status']}")
        print(f"lines={len(result['lines'])} boxes={len(result['boxes'])}")

        line_n = await conn.fetchval("SELECT COUNT(*) FROM interunit_transfers_lines WHERE header_id=$1", hid)
        box_n = await conn.fetchval("SELECT COUNT(*) FROM interunit_transfer_boxes WHERE header_id=$1", hid)
        pend = await conn.fetchrow(
            """SELECT box_id, source_table, from_site, to_site, status, net_weight, item_description
               FROM pending_transfer_stock WHERE transfer_out_id=$1""", hid)
        unalloc = await conn.fetchval(
            "SELECT unallocated_boxes FROM interunit_transfers_header WHERE id=$1", hid)

        assert result["status"] == "Dispatch", f"expected Dispatch, got {result['status']}"
        assert line_n == 1 and box_n == 1, f"persisted lines={line_n} boxes={box_n}"
        assert len(result["boxes"]) == 1 and len(result["lines"]) == 1, "detail shape wrong"
        assert pend is not None, "box NOT parked into pending_transfer_stock"
        assert pend["status"] == "In Transit", pend["status"]
        assert pend["box_id"] == synthetic_box_id, pend["box_id"]
        assert pend["source_table"], "source_table should be the guessed warehouse table"
        assert unalloc == 0, f"warehouse reconcile flag wrong: unallocated_boxes={unalloc}"
        print(f"  parked: {pend['box_id']} src={pend['source_table']} "
              f"{pend['from_site']}->{pend['to_site']} [{pend['status']}] net={pend['net_weight']}")
        print(f"  header/line/box written; pending parked (no-source branch); unallocated_boxes={unalloc}")

        # Duplicate-box guard (the "boxes collapsed to 1" protection).
        dup = schemas.TransferCreate(header=payload.header, lines=payload.lines,
                                     boxes=[payload.boxes[0], payload.boxes[0]])
        try:
            await create_service.create_transfer(conn, dup, "rollback-test@candorfoods.in")
            print("  WARN: duplicate-box guard did NOT fire")
        except HTTPException as e:
            print(f"  dup-guard fired: HTTP {e.status_code} — {str(e.detail)[:70]}")

        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        print("rolled back — nothing committed.")
        leftover = await conn.fetchval(
            "SELECT COUNT(*) FROM interunit_transfers_header WHERE remark=$1", REMARK)
        print(f"post-rollback leftover test headers (expect 0): {leftover}")
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
