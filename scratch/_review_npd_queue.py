"""NPD queue backend review (rollback-verified).

Runs inside one transaction that is rolled back, so NOTHING persists. Adds the
hold_start_date column (059) in-txn, then exercises the real services:
  - list_requisitions new filters: sample_types, q, requestor, date range
  - list_requestors facet
  - act_npd_review HOLD stores hold_start_date (+ status ON_HOLD)
Asserts, then rolls back."""
import asyncio
import datetime
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.modules.sample.services import approval_service as aps
from app.modules.sample.services import requisition_service as rs

load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
    role = await c.fetchval(
        "SELECT r.role_name FROM auth_user u JOIN auth_role r ON r.role_id=u.role_id WHERE u.user_id=$1",
        uid)
    user = types.SimpleNamespace(user_id=uid, role_name=role or "admin", full_name="Reviewer")
    print(f"using auth_user {uid} role={role}")

    pre = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    fails = []
    checks = {}
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute("ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS hold_start_date DATE")

        marker = "QUEUEHARNESS_REQUESTOR_ZZ"
        target = "QueueHarness Trail Mix ZZ"
        payload = dict(sample_type="NPD", warehouse="W202", npd_target_name=target,
                       quantity=3.0, description="queue harness description",
                       purpose_tag="TASTING_SENSORY", requestor_team=marker,
                       articles=[], internal_override=False)
        created = await rs.create_requisition(c, payload=payload, user=user)
        req_id = created["id"]

        # DRAFT -> SUBMITTED -> ON_HOLD (with start date + reason)
        await rs.submit_requisition(c, req_id, user=user)
        sd = datetime.date.today()
        held = await aps.act_npd_review(c, req_id, action="HOLD", user=user,
                                        reason="awaiting trial inputs", start_date=sd)
        checks["status ON_HOLD"] = held.get("status") == "ON_HOLD"
        checks["hold_start_date stored"] = str(held.get("hold_start_date")) == str(sd)

        # list filters
        by_types = await rs.list_requisitions(c, sample_types=["NPD", "TRIAL"], limit=200)
        checks["sample_types returns row"] = any(r["id"] == req_id for r in by_types)

        by_q = await rs.list_requisitions(c, q="QueueHarness", sample_types=["NPD", "TRIAL"], limit=200)
        checks["q matches target"] = any(r["id"] == req_id for r in by_q)

        by_qreq = await rs.list_requisitions(c, q=marker, limit=200)
        checks["q matches requestor"] = any(r["id"] == req_id for r in by_qreq)

        by_req = await rs.list_requisitions(c, requestor=marker, limit=200)
        checks["requestor exact filter"] = (
            bool(by_req) and all(r["requestor_team"] == marker for r in by_req)
            and any(r["id"] == req_id for r in by_req))

        by_date = await rs.list_requisitions(c, date_from=sd, date_to=sd,
                                             sample_types=["NPD", "TRIAL"], limit=200)
        checks["date range includes today"] = any(r["id"] == req_id for r in by_date)

        future = sd + datetime.timedelta(days=1)
        by_future = await rs.list_requisitions(c, date_from=future,
                                               sample_types=["NPD", "TRIAL"], limit=200)
        checks["date_from future excludes"] = all(r["id"] != req_id for r in by_future)

        # legacy single sample_type still works
        by_legacy = await rs.list_requisitions(c, sample_type="NPD", limit=200)
        checks["legacy sample_type works"] = any(r["id"] == req_id for r in by_legacy)

        # requestor facet
        reqs = await rs.list_requestors(c, sample_types=["NPD", "TRIAL"])
        checks["requestor facet includes marker"] = marker in reqs

        for k, v in checks.items():
            if not v:
                fails.append(k)
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()

    post = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    if post != pre:
        fails.append(f"LEAK {pre}->{post}")
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
