"""Verify update_requisition persists `description` (rollback-verified)."""
import asyncio
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.modules.sample.services import requisition_service as rs

load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
    user = types.SimpleNamespace(user_id=uid, role_name="admin", full_name="Reviewer")
    pre = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        created = await rs.create_requisition(c, payload=dict(
            sample_type="NPD", warehouse="W202", npd_target_name="DescEdit ZZ",
            quantity=2.0, description="original desc", requestor_team="QA",
            articles=[], internal_override=False), user=user)
        req_id = created["id"]
        checks["created desc"] = created.get("description") == "original desc"

        # PATCH a new description
        upd = await rs.update_requisition(c, req_id, payload={"description": "updated desc"}, user=user)
        checks["description updated"] = upd.get("description") == "updated desc"

        # omit description -> COALESCE keeps the prior value
        upd2 = await rs.update_requisition(c, req_id, payload={"quantity": 5.0}, user=user)
        checks["description preserved when omitted"] = upd2.get("description") == "updated desc"
        checks["quantity updated"] = float(upd2.get("quantity") or 0) == 5.0

        for k, v in checks.items():
            if not v:
                fails.append(k)
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()

    post = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    if post != pre:
        fails.append(f"LEAK {pre}->{post}")
    await c.close()
    print(f"post-rollback count restored: {pre} == {post}")
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)
    print("PASS")


asyncio.run(main())
