"""Rollback integration test for export data. Run:
    PYTHONPATH=. python tests/services/test_cr_export_rollback.py
"""
import asyncio
import asyncpg
from decimal import Decimal
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, box_service, query_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        created = await create_service.create_cr(
            conn, "CFPL",
            schemas.CRCreate(company="CFPL",
                header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ZZ_EXPORT_CO"),
                lines=[schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                        item_description="ALMOND W-320", uom="KG", qty="3", rate="10")]),
            "t@x.in")
        cr_id = created["rtv_id"]
        r = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=1,
            net_weight=Decimal("25.000"), gross_weight=Decimal("26.000"), lot_number="LOTX", count=40))
        box_id = r["box_id"]

        rows = await query_service.export_cr_records(conn, company="CFPL", customer="zz_export_co")
        assert rows, "expected at least one export row"
        row = rows[0]
        assert list(row.keys()) == query_service.EXPORT_COLUMNS       # exact 33-col contract
        assert row["RTV ID"] == cr_id and row["Customer"] == "ZZ_EXPORT_CO"
        assert row["Item Description"] == "ALMOND W-320" and row["Qty"] == 3.0
        assert row["Box ID"] == box_id and row["Box Net Weight"] == 25.0 and row["Box Count"] == 40

        # edited-cells lookup
        await box_service.log_box_edits(conn,
            schemas.CRBoxEditLogRequest(email_id="x", box_id=box_id, rtv_id=cr_id,
                changes=[schemas.CRBoxEditLogEntry(field_name="net_weight", old_value="25", new_value="24")]),
            email_id="e@e.in")
        edited = await query_service.get_edited_cells(conn, [cr_id])
        assert (box_id, "net_weight") in edited
        assert await query_service.get_edited_cells(conn, []) == set()
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
