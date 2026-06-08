"""P3a review harness. Reads run against live DB; deletes are exercised only via
their 404 (no-match) paths so NO real rows are mutated. Permission asserts are
unit-tested with synthetic AuthUsers."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv
from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.modules.auth.middleware import AuthUser
from app.modules.transfer import permissions, schemas
from app.modules.transfer.services import pending_service, delete_service, inner_cold_service

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        print(f"  PASS  {name} {extra}")
        ok += 1
    else:
        print(f"  FAIL  {name} {extra}")
        fail += 1


def _user(email="x@x.com", role="", admin=False):
    return AuthUser(user_id=1, phone="", full_name="", email=email, entity="",
                    role_id=1, role_name=role, is_admin=admin)


def _raises_403(fn):
    try:
        fn()
        return False
    except AuthError as e:
        return e.status_code == 403 if hasattr(e, "status_code") else True
    except Exception:
        return False


async def _raises_http(coro, status):
    try:
        await coro
        return False
    except HTTPException as e:
        return e.status_code == status


async def main():
    # ── Permission asserts (no DB) ──
    print("Permissions:")
    check("admin can delete inner-cold", permissions.assert_can_delete_inner_cold(_user(admin=True)) is not None)
    check("hrithik can delete inner-cold", permissions.assert_can_delete_inner_cold(_user(email="hrithik@candorfoods.in")) is not None)
    check("random CANNOT delete inner-cold", _raises_403(lambda: permissions.assert_can_delete_inner_cold(_user(email="bob@x.com", role="qc_inspector"))))
    check("yash can delete transfer-in", permissions.assert_can_delete_transfer_in(_user(email="yash@candorfoods.in")) is not None)
    check("hrithik CANNOT delete transfer-in", _raises_403(lambda: permissions.assert_can_delete_transfer_in(_user(email="hrithik@candorfoods.in"))))
    check("b.hrithik can delete transfer", permissions.assert_can_delete_transfer(_user(email="b.hrithik@candorfoods.in")) is not None)
    check("developer-role can backfill", permissions.assert_can_backfill(_user(role="developer")) is not None)
    check("random CANNOT delete request", _raises_403(lambda: permissions.assert_can_delete_request(_user(email="bob@x.com"))))

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        print("Reads (live DB):")
        # pending
        p = await pending_service.list_pending_transfers(conn)
        check("pending has records+filter_options keys",
              {"records", "total", "filter_options"} <= set(p.keys()),
              f"total={p['total']}")
        if p["records"]:
            check("pending record validates schema",
                  schemas.PendingTransferRecord.model_validate(p["records"][0]) is not None)
        check("pending filter_options has chips+counts",
              {"from_sites", "to_sites", "from_site_counts", "to_site_counts"} <= set(p["filter_options"].keys()))

        # inner cold
        ic = await inner_cold_service.list_inner_cold(conn, page=1, per_page=5)
        check("inner-cold validates schema",
              schemas.InnerColdListResponse.model_validate(ic) is not None,
              f"total={ic['total']}")

        print("Deletes (safe / no-match paths):")
        check("delete_request(-1) -> 404", await _raises_http(delete_service.delete_request(conn, -1), 404))
        check("delete_inner_cold(bogus) -> 404", await _raises_http(inner_cold_service.delete_inner_cold(conn, "__nope__"), 404))

        print("Destructive trio deferred -> 501:")
        check("backfill -> 501", await _raises_http(pending_service.backfill_pending_from_existing_transfers(conn), 501))
        check("delete_transfer -> 501", await _raises_http(delete_service.delete_transfer(conn, 1), 501))
        check("delete_transfer_in -> 501", await _raises_http(delete_service.delete_transfer_in(conn, 1, "x"), 501))
    finally:
        await conn.close()

    print(f"\nP3a REVIEW: {ok} passed, {fail} failed")


asyncio.run(main())
