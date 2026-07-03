"""Rollback integration test: create_cr + get_cr against the real DB. All writes
are rolled back — safe against prod. Run:
    PYTHONPATH=. python tests/services/test_cr_create_rollback.py
"""
import asyncio
import asyncpg
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        payload = schemas.CRCreate(
            company="CFPL",
            header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME FOODS",
                                          conversion="1.5", business_head="Head One"),
            lines=[
                schemas.CRLineCreate(material_type="rm", item_category="Nuts",
                                     sub_category="Almond", item_description="ALMOND W-320",
                                     uom="kg", qty="4", rate="10"),  # value auto = 40
                schemas.CRLineCreate(material_type="rm", item_category="Nuts",
                                     sub_category="Cashew", item_description="CASHEW W-240",
                                     uom="kg", qty="2", rate="20", value="45"),
            ],
        )
        created = await create_service.create_cr(conn, "CFPL", payload,
                                                  "tester@candorfoods.in")
        cr_id = created["rtv_id"]
        assert cr_id.startswith("CR-"), cr_id
        assert created["status"] == "Pending"
        assert created["created_by"] == "tester@candorfoods.in"
        assert len(created["lines"]) == 2 and created["boxes"] == []

        fetched = await query_service.get_cr(conn, "CFPL", cr_id)
        assert fetched["rtv_id"] == cr_id
        by_desc = {l["item_description"]: l for l in fetched["lines"]}
        assert by_desc["ALMOND W-320"]["value"] == "40"   # computed qty*rate
        assert by_desc["CASHEW W-240"]["value"] == "45"   # supplied
        assert by_desc["ALMOND W-320"]["material_type"] == "RM"  # uppercased

        # company path/body mismatch is rejected (Fix 2)
        from fastapi import HTTPException as _HTTPExc
        try:
            await create_service.create_cr(conn, "CDPL", payload, "tester@candorfoods.in")
            raise AssertionError("expected 400 on company mismatch")
        except _HTTPExc as e:
            assert e.status_code == 400 and e.detail["error"] == "company_mismatch"
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
