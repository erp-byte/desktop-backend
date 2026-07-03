"""Rollback integration test for box_service.upsert_box. Run:
    PYTHONPATH=. python tests/services/test_cr_upsert_box_rollback.py
"""
import asyncio
import asyncpg
from decimal import Decimal
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service, box_service


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

        # 1) insert a new box → box_id 2-part, status inserted
        r1 = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=1,
            net_weight=Decimal("25.000"), gross_weight=Decimal("26.000"), lot_number="LOT1"))
        assert r1["status"] == "inserted"
        assert r1["box_id"].endswith("-1") and r1["box_id"].count("-") == 1, r1["box_id"]
        first_box_id = r1["box_id"]

        # 2) re-upsert same box → box_id preserved, COALESCE keeps lot when None passed
        r2 = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=1, net_weight=Decimal("24.500")))
        assert r2["status"] == "updated" and r2["box_id"] == first_box_id
        got = await query_service.get_cr(conn, "CFPL", cr_id)
        b = next(x for x in got["boxes"] if x["box_number"] == 1)
        assert b["net_weight"] == "24.5"          # updated
        assert b["lot_number"] == "LOT1"          # preserved (None in payload didn't clear it)
        assert b["box_id"] == first_box_id

        # 3) new box_number → separate insert
        r3 = await box_service.upsert_box(conn, "CFPL", cr_id, schemas.CRBoxUpsertRequest(
            article_description="ALMOND W-320", box_number=2, net_weight=Decimal("25.000")))
        assert r3["status"] == "inserted" and r3["box_id"].endswith("-2")

        # 4) missing CR → 404
        from fastapi import HTTPException
        try:
            await box_service.upsert_box(conn, "CFPL", "CR-DOESNOTEXIST",
                schemas.CRBoxUpsertRequest(article_description="X", box_number=1))
            raise AssertionError("expected 404")
        except HTTPException as e:
            assert e.status_code == 404
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
