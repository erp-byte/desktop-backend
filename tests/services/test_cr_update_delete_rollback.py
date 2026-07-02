"""Rollback integration test: update_cr, update_cr_lines, delete_cr. Run:
    PYTHONPATH=. python tests/services/test_cr_update_delete_rollback.py
"""
import asyncio
import asyncpg
from fastapi import HTTPException
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        created = await create_service.create_cr(
            conn, "CFPL",
            schemas.CRCreate(company="CFPL",
                             header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                             lines=[schemas.CRLineCreate(material_type="RM", item_category="N",
                                     sub_category="S", item_description="ALMOND W-320",
                                     uom="KG", qty="1", rate="1")]),
            "t@x.in")
        cr_id = created["rtv_id"]

        # header update
        upd = await create_service.update_cr(conn, "CFPL", cr_id,
                                             schemas.CRHeaderUpdate(remark="edited", status="Submitted"))
        assert upd["remark"] == "edited" and upd["status"] == "Submitted"

        # empty update -> 400
        try:
            await create_service.update_cr(conn, "CFPL", cr_id, schemas.CRHeaderUpdate())
            raise AssertionError("expected 400 empty update")
        except HTTPException as e:
            assert e.status_code == 400

        # replace lines
        res = await create_service.update_cr_lines(
            conn, "CFPL", cr_id,
            schemas.CRLinesUpdateRequest(lines=[
                schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                                     item_description="CASHEW W-240", uom="KG", qty="2", rate="5"),
            ]))
        assert res["lines_count"] == 1
        fetched = await query_service.get_cr(conn, "CFPL", cr_id)
        assert [l["item_description"] for l in fetched["lines"]] == ["CASHEW W-240"]

        # delete (cascades lines)
        d = await create_service.delete_cr(conn, "CFPL", cr_id)
        assert d["success"] and d["lines_count"] == 1
        try:
            await query_service.get_cr(conn, "CFPL", cr_id)
            raise AssertionError("expected 404 after delete")
        except HTTPException as e:
            assert e.status_code == 404
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
