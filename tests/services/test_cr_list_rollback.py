"""Rollback integration test: list_crs filtering/pagination/aggregates. Run:
    PYTHONPATH=. python tests/services/test_cr_list_rollback.py
"""
import asyncio
import asyncpg
from app.config import Settings
from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import create_service, query_service


def _mk(customer: str) -> schemas.CRCreate:
    return schemas.CRCreate(
        company="CFPL",
        header=schemas.CRHeaderCreate(factory_unit="A-185", customer=customer),
        lines=[schemas.CRLineCreate(material_type="RM", item_category="N", sub_category="S",
                                    item_description="ALMOND W-320", uom="KG", qty="3", rate="10")],
    )


async def main() -> None:
    conn = await asyncpg.connect(Settings().DATABASE_URL, timeout=10)
    tx = conn.transaction()
    await tx.start()
    try:
        a = await create_service.create_cr(conn, "CFPL", _mk("ZZ_TEST_ACME"), "t@x.in")
        b = await create_service.create_cr(conn, "CFPL", _mk("ZZ_TEST_BETA"), "t@x.in")

        # customer ILIKE filter finds only ACME.
        res = await query_service.list_crs(conn, company="CFPL", page=1, per_page=10,
                                           customer="zz_test_acme")
        ids = {r["rtv_id"] for r in res["records"]}
        assert a["rtv_id"] in ids and b["rtv_id"] not in ids, ids
        row = next(r for r in res["records"] if r["rtv_id"] == a["rtv_id"])
        assert row["items_count"] == 1 and row["total_qty"] == 3 and row["boxes_count"] == 0
        assert res["total"] >= 1 and res["page"] == 1 and res["per_page"] == 10

        # per_page pagination math.
        res2 = await query_service.list_crs(conn, company="CFPL", page=1, per_page=1,
                                            customer="zz_test_")
        assert len(res2["records"]) == 1 and res2["total"] >= 2 and res2["total_pages"] >= 2
        print("ASSERTIONS PASSED")
    finally:
        await tx.rollback()
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
