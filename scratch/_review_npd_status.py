"""NPD status-bucket filter + hold_reason in list (rollback-verified)."""
import asyncio
import datetime
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.modules.sample.services import approval_service as aps
from app.modules.sample.services import requisition_service as rs

load_dotenv()

PENDING = ["DRAFT", "SUBMITTED"]
HOLD = ["ON_HOLD"]
ACCEPTED = ["BH_APPROVED", "IN_PRODUCTION", "PACKING", "READY_FOR_DISPATCH",
            "INTERNALLY_DISPATCHED", "PARTIALLY_CONVERTED", "GATE_PASS_ISSUED", "CLOSED"]


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
    user = types.SimpleNamespace(user_id=uid, role_name="admin", full_name="Reviewer")
    pre = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS hold_start_date DATE")
        created = await rs.create_requisition(c, payload=dict(
            sample_type="NPD", warehouse="W202", npd_target_name="StatusBucket ZZ",
            quantity=2.0, requestor_team="QA", articles=[], internal_override=False), user=user)
        req_id = created["id"]

        def has(rows):
            return any(r["id"] == req_id for r in rows)

        # DRAFT -> Pending bucket includes it
        checks["pending incl draft"] = has(await rs.list_requisitions(c, statuses=PENDING, limit=200))
        checks["hold bucket excl draft"] = not has(await rs.list_requisitions(c, statuses=HOLD, limit=200))

        await rs.submit_requisition(c, req_id, user=user)
        checks["pending incl submitted"] = has(await rs.list_requisitions(c, statuses=PENDING, limit=200))

        # HOLD with reason
        reason = "QC sample bucket reason"
        await aps.act_npd_review(c, req_id, action="HOLD", user=user, reason=reason,
                                 start_date=datetime.date.today())
        hold_rows = await rs.list_requisitions(c, statuses=HOLD, limit=200)
        checks["hold bucket incl on_hold"] = has(hold_rows)
        row = next((r for r in hold_rows if r["id"] == req_id), None)
        checks["hold_reason returned"] = bool(row) and row["hold_reason"] == reason
        checks["pending excl on_hold"] = not has(await rs.list_requisitions(c, statuses=PENDING, limit=200))

        # Accept -> BH_APPROVED -> Accepted bucket
        await aps.act_npd_review(c, req_id, action="APPROVE", user=user)
        checks["accepted incl approved"] = has(await rs.list_requisitions(c, statuses=ACCEPTED, limit=200))
        checks["hold bucket excl approved"] = not has(await rs.list_requisitions(c, statuses=HOLD, limit=200))

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
