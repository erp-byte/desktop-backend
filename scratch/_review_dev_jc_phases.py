"""Dev job-card 8-digit id + phase lifecycle (rollback-verified).

Creates the phases table in-txn, then exercises the real npd_dev_service:
  - create_dev_job_card → id is an 8-digit time-based number (new_short_time_id)
  - add_phase / start_phase / complete_phase lifecycle + timestamps
Rolls back so nothing persists. (id stays int4 here — 8-digit fits int4; the
BIGINT migration 060 is validated separately by running the DDL.)"""
import asyncio
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.modules.sample.services import npd_dev_service as svc

load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
    user = types.SimpleNamespace(user_id=uid, role_name="admin", is_admin=True, full_name="Reviewer")
    pre = await c.fetchval("SELECT COUNT(*) FROM npd_dev_job_cards")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        await c.execute(
            """CREATE TABLE IF NOT EXISTS npd_dev_job_card_phases (
                 phase_id BIGINT PRIMARY KEY,
                 dev_jc_id INT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
                 phase_number INT NOT NULL,
                 name TEXT NOT NULL,
                 status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','IN_PROGRESS','COMPLETED')),
                 started_at TIMESTAMPTZ, started_by INT,
                 completed_at TIMESTAMPTZ, completed_by INT,
                 notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                 UNIQUE (dev_jc_id, phase_number))""")

        jc = await svc.create_dev_job_card(c, payload={
            "title": "Phase Harness Trial",
            "lines": [{"sku_name": "Test RM", "qty": 1, "uom": "kg", "item_type": "rm"}],
        }, user=user)
        jid = jc["id"]
        checks["id is 8-digit"] = isinstance(jid, int) and 10_000_000 <= jid <= 99_999_999
        checks["phases empty on create"] = jc.get("phases") == []

        await svc.start_development(c, jid, user=user)

        jc = await svc.add_phase(c, jid, name="Trial batch 1", user=user)
        checks["phase added PENDING"] = (len(jc["phases"]) == 1
                                         and jc["phases"][0]["status"] == "PENDING"
                                         and jc["phases"][0]["phase_number"] == 1)
        pid = jc["phases"][0]["phase_id"]
        checks["phase_id is 8-digit"] = isinstance(pid, int) and 10_000_000 <= pid <= 99_999_999

        jc = await svc.start_phase(c, jid, pid, user=user)
        p0 = jc["phases"][0]
        checks["phase IN_PROGRESS + started_at"] = (p0["status"] == "IN_PROGRESS"
                                                    and p0["started_at"] is not None)

        jc = await svc.complete_phase(c, jid, pid, notes="batch ran 3 days", user=user)
        p0 = jc["phases"][0]
        checks["phase COMPLETED + completed_at + notes"] = (
            p0["status"] == "COMPLETED" and p0["completed_at"] is not None
            and p0["notes"] == "batch ran 3 days")

        jc = await svc.add_phase(c, jid, name="Sensory evaluation", user=user)
        checks["phase_number increments"] = jc["phases"][1]["phase_number"] == 2

        for k, v in checks.items():
            if not v:
                fails.append(k)
        print("id:", jid)
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()

    post = await c.fetchval("SELECT COUNT(*) FROM npd_dev_job_cards")
    if post != pre:
        fails.append(f"LEAK {pre}->{post}")
    await c.close()
    print(f"post-rollback count restored: {pre} == {post}")
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)
    print("PASS")


asyncio.run(main())
