"""NPD sample-requisition create review (rollback-verified).

Runs entirely inside one transaction that is rolled back, so NOTHING persists:
adds the `description` column (058) in-txn, exercises the real
requisition_service.create_requisition via an NPD-shaped payload, asserts the
stored row, then rolls back (dropping the column + row). Also unit-checks the
NpdRequisitionCreate Pydantic validation (no DB)."""
import asyncio
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.modules.sample import schemas
from app.modules.sample.services import requisition_service as rs

load_dotenv()


def check_pydantic():
    fails = []
    # valid
    ok = schemas.NpdRequisitionCreate(sample_type="NPD", npd_target_name="Trail Mix 200g",
                                      quantity=12.5, warehouse="W202")
    if ok.sample_type != "NPD":
        fails.append("valid parse wrong")
    # required / enum failures
    from pydantic import ValidationError
    for bad, label in [
        (dict(sample_type="BASIS_RM", npd_target_name="x", quantity=1, warehouse="W202"), "sample_type enum"),
        (dict(sample_type="NPD", npd_target_name="", quantity=1, warehouse="W202"), "empty target"),
        (dict(sample_type="NPD", npd_target_name="x", quantity=0, warehouse="W202"), "quantity>0"),
        (dict(sample_type="NPD", npd_target_name="x", quantity=1, warehouse="D-39"), "warehouse subset"),
        (dict(sample_type="NPD", npd_target_name="x", quantity=1), "warehouse required"),
    ]:
        try:
            schemas.NpdRequisitionCreate(**bad)
            fails.append(f"should have rejected: {label}")
        except ValidationError:
            pass
    return fails


async def main():
    fails = check_pydantic()
    print(f"pydantic checks: {'ok' if not fails else fails}")

    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
    role = await c.fetchval("SELECT r.role_name FROM auth_user u JOIN auth_role r ON r.role_id=u.role_id WHERE u.user_id=$1", uid)
    user = types.SimpleNamespace(user_id=uid, role_name=role or "admin", full_name="Reviewer")
    print(f"using auth_user {uid} role={role}")

    pre = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS description TEXT")
        body = schemas.NpdRequisitionCreate(
            sample_type="NPD", npd_target_name="Premia Trail Mix 200g", quantity=12.5,
            warehouse="W202", description="low-salt cashew base trial", purpose_tag="TASTING_SENSORY",
            requestor_team="NPD",
        )
        payload = body.model_dump()
        payload["articles"] = []
        payload["internal_override"] = False
        res = await rs.create_requisition(c, payload=payload, user=user)

        checks = {
            "request_id present": bool(res.get("request_id")),
            "request_id 8-digit": len(str(res.get("request_id") or "")) == 8,
            "status DRAFT": res.get("status") == "DRAFT",
            "sample_type NPD": res.get("sample_type") == "NPD",
            "npd_target_name": res.get("npd_target_name") == "PREMIA TRAIL MIX 200G" or res.get("npd_target_name") == "Premia Trail Mix 200g",
            "quantity 12.5": float(res.get("quantity") or 0) == 12.5,
            "description stored": res.get("description") == "low-salt cashew base trial",
            "warehouse W202": res.get("warehouse") == "W202",
            "purpose_tag": res.get("purpose_tag") == "TASTING_SENSORY",
            "requestor_user_id=user": res.get("requestor_user_id") == uid,
            "no articles": len(res.get("articles") or []) == 0,
        }
        for k, v in checks.items():
            if not v:
                fails.append(f"{k} -> {v} (got {res.get(k.split()[0]) if False else ''})")
        print("created row:", {k: res.get(k) for k in ("request_id", "status", "sample_type", "npd_target_name", "quantity", "description", "warehouse", "purpose_tag", "requestor_user_id")})
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()

    post = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    if post != pre:
        fails.append(f"LEAK: {pre} -> {post}")
    print(f"post-rollback count restored: {pre} == {post}")
    await c.close()

    print()
    if fails:
        print("FAIL:")
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print("PASS: all checks green")


asyncio.run(main())
