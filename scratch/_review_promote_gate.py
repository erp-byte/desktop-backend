"""Blocking dual-approval gate on the NPD dev-JC promote (rollback-verified).

REQUIRES: DATABASE_URL in the environment AND migrations 069 + 070 applied
(npd_dev_promote_request / npd_dev_promote_approval). There is no DB in CI here,
so a human runs this:  python -m scratch._review_promote_gate  (from server_replica).

What it proves (all inside one transaction, rolled back at the end):
  - request_promote() on an IN_DEVELOPMENT dev JC linked to a requisition with a
    known requestor_user_id opens exactly ONE pending npd_dev_promote_request and
    TWO pending npd_dev_promote_approval rows (INV_MGR + REQUESTOR_BH), and the JC
    stays IN_DEVELOPMENT (promote did NOT run yet).
  - act_promote_approval() as an inventory_manager (ACCEPT) leaves the request
    PENDING_APPROVAL (one gate still open).
  - act_promote_approval() as the requestor (ACCEPT) finalizes: the JC flips to
    CLOSED with promoted_bom_id set and the request goes APPROVED.
Rolls back so nothing persists."""
import asyncio
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.core.helpers import new_short_time_id
from app.modules.sample.services import npd_dev_service as svc
from app.modules.sample.services import promote_approval_service as pas

load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    pre = await c.fetchval("SELECT COUNT(*) FROM npd_dev_job_cards")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        # ── seed: pick two real users — one inventory_manager, one requestor BH ──
        inv_role = await c.fetchval("SELECT role_id FROM auth_role WHERE role_name = 'inventory_manager'")
        if inv_role is None:
            inv_role = await c.fetchval("SELECT role_id FROM auth_role ORDER BY role_id LIMIT 1")
        uids = await c.fetch("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 2")
        inv_uid = uids[0]["user_id"]
        req_uid = uids[1]["user_id"] if len(uids) > 1 else inv_uid
        # Make inv_uid a real inventory_manager so the role-gate accepts it.
        await c.execute("UPDATE auth_user SET role_id = $2 WHERE user_id = $1", inv_uid, inv_role)

        # A source requisition whose requestor is req_uid (its BH gate approver).
        # request_id is the app-minted 8-digit PK (requisition_number was dropped in
        # migration 068) — mirror requisition_service's INSERT.
        src_req = await c.fetchval(
            """INSERT INTO sample_requisitions
                   (request_id, sample_type, status, requestor_user_id, entity, created_by)
               VALUES ($1, 'NPD', 'BH_APPROVED', $2, 'cfpl', $2) RETURNING id""",
            new_short_time_id(), req_uid)

        # An IN_DEVELOPMENT dev JC linked to that requisition, with a base recipe.
        dev_jc_id = new_short_time_id()
        await c.execute(
            """INSERT INTO npd_dev_job_cards
                   (id, dev_jc_number, title, fg_sku_name, uom, source_requisition_id,
                    status, created_by)
               VALUES ($1, $2, 'Promote gate trial', 'PROMOTE GATE FG', 'kg', $3,
                       'IN_DEVELOPMENT', $4)""",
            dev_jc_id, f"NPDJC-PROMOTEGATE-{os.getpid()}", src_req, req_uid)
        await c.execute(
            """INSERT INTO npd_dev_job_card_lines
                   (dev_jc_id, phase_id, sku_name, qty, uom, item_type, line_order)
               VALUES ($1, NULL, 'Salt', 1.0, 'kg', 'rm', 0)""",
            dev_jc_id)

        # Users the service sees. The operator who requests is an admin (bypasses
        # the npd_authorized_users allow-list cleanly).
        operator = types.SimpleNamespace(user_id=req_uid, role_name="npd_team",
                                         is_admin=True, full_name="Operator")
        inv_user = types.SimpleNamespace(user_id=inv_uid, role_name="inventory_manager",
                                         is_admin=False, full_name="Inv Mgr")
        bh_user = types.SimpleNamespace(user_id=req_uid, role_name="npd_team",
                                        is_admin=False, full_name="Requestor BH")

        # ── 1) request_promote opens the gate (does NOT promote yet) ──
        res = await svc.request_promote(c, dev_jc_id, payload={"promote_phase_id": None}, user=operator)
        n_req = await c.fetchval(
            "SELECT COUNT(*) FROM npd_dev_promote_request WHERE dev_jc_id=$1 AND status='PENDING'", dev_jc_id)
        pr_id = await c.fetchval(
            "SELECT id FROM npd_dev_promote_request WHERE dev_jc_id=$1 AND status='PENDING'", dev_jc_id)
        n_appr = await c.fetchval(
            "SELECT COUNT(*) FROM npd_dev_promote_approval WHERE promote_request_id=$1 AND status='PENDING'", pr_id)
        kinds = sorted([r["approver_kind"] for r in await c.fetch(
            "SELECT approver_kind FROM npd_dev_promote_approval WHERE promote_request_id=$1", pr_id)])
        bh_appr_uid = await c.fetchval(
            "SELECT approver_user_id FROM npd_dev_promote_approval "
            "WHERE promote_request_id=$1 AND approver_kind='REQUESTOR_BH'", pr_id)
        st_after_req = await c.fetchval("SELECT status FROM npd_dev_job_cards WHERE id=$1", dev_jc_id)
        checks["request_promote → 1 PENDING request"] = n_req == 1
        checks["request_promote → 2 PENDING approvals"] = n_appr == 2
        checks["gates are INV_MGR + REQUESTOR_BH"] = kinds == ["INV_MGR", "REQUESTOR_BH"]
        checks["REQUESTOR_BH bound to the requisition requestor"] = bh_appr_uid == req_uid
        checks["JC still IN_DEVELOPMENT (promote not run)"] = st_after_req == "IN_DEVELOPMENT"
        checks["request returns PENDING_APPROVAL"] = res.get("status") == "PENDING_APPROVAL"

        # ── 2) inventory_manager ACCEPT → still blocked on the BH gate ──
        r_inv = await pas.act_promote_approval(c, dev_jc_id, action="ACCEPT", user=inv_user, remarks="inv ok")
        st_mid = await c.fetchval("SELECT status FROM npd_dev_job_cards WHERE id=$1", dev_jc_id)
        inv_gate = await c.fetchval(
            "SELECT status FROM npd_dev_promote_approval WHERE promote_request_id=$1 AND approver_kind='INV_MGR'", pr_id)
        checks["INV_MGR accept → still PENDING_APPROVAL"] = r_inv.get("status") == "PENDING_APPROVAL"
        checks["INV_MGR gate now ACCEPTED"] = inv_gate == "ACCEPTED"
        checks["JC still IN_DEVELOPMENT after one accept"] = st_mid == "IN_DEVELOPMENT"

        # ── 3) requestor BH ACCEPT → both gates clear → promote finalizes ──
        r_bh = await pas.act_promote_approval(c, dev_jc_id, action="ACCEPT", user=bh_user, remarks="bh ok")
        jc = await c.fetchrow("SELECT status, promoted_bom_id FROM npd_dev_job_cards WHERE id=$1", dev_jc_id)
        req_final = await c.fetchval("SELECT status FROM npd_dev_promote_request WHERE id=$1", pr_id)
        checks["BH accept → PROMOTED"] = r_bh.get("status") == "PROMOTED"
        checks["JC now CLOSED"] = jc["status"] == "CLOSED"
        checks["JC has promoted_bom_id set"] = jc["promoted_bom_id"] is not None
        checks["promote request now APPROVED"] = req_final == "APPROVED"

        for k, v in checks.items():
            if not v:
                fails.append(k)
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()

    post = await c.fetchval("SELECT COUNT(*) FROM npd_dev_job_cards")
    if post != pre:
        fails.append(f"LEAK {pre}->{post}")
    await c.close()
    print(f"count restored: {pre} == {post}")
    n = len(checks)
    passed = n - len([k for k in checks if not checks[k]])
    print(f"{passed}/{n} PASS")
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)


asyncio.run(main())
