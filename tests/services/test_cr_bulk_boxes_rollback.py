"""Rollback integration test for box_service.bulk_save_boxes. Run:
    PYTHONPATH=. python tests/services/test_cr_bulk_boxes_rollback.py
"""
import asyncio
import asyncpg
from decimal import Decimal
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service, box_service


def _item(art, num, nw):
    return schemas.CRBulkBoxItem(article_description=art, box_number=num, net_weight=Decimal(nw))


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        created = await create_service.create_cr(
            conn, "CFPL",
            schemas.CRCreate(company="CFPL",
                header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                lines=[schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                        item_description="ALMOND W-320", uom="KG", qty="1", rate="1")]),
            "t@x.in")
        cr_id = created["rtv_id"]

        # first sync: 2 boxes inserted, 3-part box_ids
        r1 = await box_service.bulk_save_boxes(conn, "CFPL", cr_id,
            schemas.CRBulkBoxUpdateRequest(boxes=[
                _item("ALMOND W-320", 1, "25.0"), _item("ALMOND W-320", 2, "25.0")]))
        assert r1 == {"status": "synced", "rtv_id": cr_id, "inserted": 2,
                      "updated": 0, "unchanged": 0, "deleted": 0}, r1
        got = await query_service.get_cr(conn, "CFPL", cr_id)
        b1 = next(x for x in got["boxes"] if x["box_number"] == 1)
        assert b1["box_id"].count("-") == 2, b1["box_id"]   # three-part
        b1_id = b1["box_id"]

        # second sync: box1 kept+changed (update, box_id preserved), box2 dropped (delete), box3 new (insert)
        r2 = await box_service.bulk_save_boxes(conn, "CFPL", cr_id,
            schemas.CRBulkBoxUpdateRequest(boxes=[
                _item("ALMOND W-320", 1, "24.0"), _item("ALMOND W-320", 3, "25.0")]))
        assert r2["inserted"] == 1 and r2["updated"] == 1 and r2["deleted"] == 1, r2
        got2 = await query_service.get_cr(conn, "CFPL", cr_id)
        nums = sorted(x["box_number"] for x in got2["boxes"])
        assert nums == [1, 3], nums
        b1b = next(x for x in got2["boxes"] if x["box_number"] == 1)
        assert b1b["box_id"] == b1_id and b1b["net_weight"] == "24"   # preserved id, updated weight

        # status flip only from Approved/Submitted: Pending CR stays Pending
        assert got2["status"] == "Pending"
        await conn.execute(
            "UPDATE cfpl_customer_return_header SET status='Approved' WHERE rtv_id=$1", cr_id)
        await box_service.bulk_save_boxes(conn, "CFPL", cr_id,
            schemas.CRBulkBoxUpdateRequest(boxes=[_item("ALMOND W-320", 1, "24.0")]))
        st = await conn.fetchval(
            "SELECT status FROM cfpl_customer_return_header WHERE rtv_id=$1", cr_id)
        assert st == "Submitted", st
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
