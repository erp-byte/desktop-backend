"""Rollback integration test for box_service.log_box_edits. Run:
    PYTHONPATH=. python tests/services/test_cr_box_edit_log_rollback.py
"""
import asyncio
import asyncpg
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import box_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        payload = schemas.CRBoxEditLogRequest(
            email_id="ignored@spoof.in", box_id="50123456-1", rtv_id="CR-TESTLOG",
            changes=[
                schemas.CRBoxEditLogEntry(field_name="net_weight", old_value="25", new_value="24"),
                schemas.CRBoxEditLogEntry(field_name="lot_number", old_value="L1", new_value="L2"),
            ])
        res = await box_service.log_box_edits(conn, payload, email_id="real@candorfoods.in")
        assert res == {"status": "logged", "entries": 2}, res

        rows = await conn.fetch(
            "SELECT email_id, description, transaction_no, box_id, field_name, old_value, new_value "
            "FROM box_edit_logs WHERE transaction_no = $1 ORDER BY field_name", "CR-TESTLOG")
        assert len(rows) == 2
        nw = next(r for r in rows if r["field_name"] == "net_weight")
        assert nw["email_id"] == "real@candorfoods.in"          # JWT actor, not payload.email_id
        assert nw["transaction_no"] == "CR-TESTLOG" and nw["box_id"] == "50123456-1"
        assert nw["description"] == "Changed net_weight from '25' to '24'"
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
