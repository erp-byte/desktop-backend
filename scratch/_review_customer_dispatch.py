"""Customer + dispatch fields end-to-end (rollback-verified).

Adds the 064 columns in-txn, then exercises the real services:
  - create requisition with customer/dispatch fields
  - update requisition (partial; COALESCE keeps the rest)
  - create dev job card from the requisition → inherits all fields
  - close the job card → confirmed dispatch date = close date (mirrored to the req)
Rolls back so nothing persists."""
import asyncio
import os
import types
from datetime import date

import asyncpg
from dotenv import load_dotenv

from app.modules.sample.services import requisition_service as rsvc
from app.modules.sample.services import npd_dev_service as dsvc

load_dotenv()

_COLS = ("company_name TEXT", "customer_name TEXT", "customer_contact TEXT",
         "customer_ship_to_address TEXT", "mode_of_transport TEXT",
         "expected_dispatch_date DATE", "confirmed_dispatch_date DATE",
         "pcs NUMERIC(15,3)", "weight_per_piece NUMERIC(15,4)")


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
    user = types.SimpleNamespace(user_id=uid, role_name="admin", is_admin=True, full_name="Reviewer")
    pre_req = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    pre_jc = await c.fetchval("SELECT COUNT(*) FROM npd_dev_job_cards")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        for tbl in ("sample_requisitions", "npd_dev_job_cards"):
            for col in _COLS:
                await c.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col}")

        req = await rsvc.create_requisition(c, payload={
            "sample_type": "NPD", "warehouse": "W202",
            "npd_target_name": "Cust Dispatch Test", "pcs": 10, "weight_per_piece": 0.5,
            "company_name": "Acme Foods", "customer_name": "Acme Retail",
            "customer_contact": "9999", "customer_ship_to_address": "12 MG Road",
            "mode_of_transport": "Road", "expected_dispatch_date": date(2026, 7, 1),
            "articles": [],
        }, user=user)
        rid = req["id"]
        checks["req stores customer/dispatch"] = (
            req["company_name"] == "Acme Foods" and req["customer_name"] == "Acme Retail"
            and req["customer_contact"] == "9999"
            and req["customer_ship_to_address"] == "12 MG Road"
            and req["mode_of_transport"] == "Road"
            and str(req["expected_dispatch_date"]) == "2026-07-01")
        checks["quantity = pcs x weight_per_piece"] = (
            float(req["pcs"]) == 10 and float(req["weight_per_piece"]) == 0.5
            and float(req["quantity"]) == 5.0)

        req = await rsvc.update_requisition(c, rid, payload={
            "customer_contact": "8888", "confirmed_dispatch_date": date(2026, 7, 5)}, user=user)
        checks["req update (partial, COALESCE keeps rest)"] = (
            req["customer_contact"] == "8888"
            and str(req["confirmed_dispatch_date"]) == "2026-07-05"
            and req["company_name"] == "Acme Foods")

        jc = await dsvc.create_dev_job_card(c, payload={
            "title": "Cust JC", "source_requisition_id": rid,
            "lines": [{"sku_name": "X", "qty": 1, "uom": "kg", "item_type": "rm"}]}, user=user)
        jid = jc["id"]
        checks["jc inherits from requisition"] = (
            jc["company_name"] == "Acme Foods" and jc["customer_name"] == "Acme Retail"
            and jc["customer_contact"] == "8888"
            and str(jc["expected_dispatch_date"]) == "2026-07-01"
            and str(jc["confirmed_dispatch_date"]) == "2026-07-05"
            and float(jc["pcs"]) == 10 and float(jc["weight_per_piece"]) == 0.5)

        # Confirmed dispatch date = the job card's CLOSING date (set on close and
        # mirrored back onto the source requisition).
        today = await c.fetchval("SELECT CURRENT_DATE")
        await dsvc.start_development(c, jid, user=user)
        jc = await dsvc.close_dev_job_card(c, jid, payload={}, user=user)
        req_after = await rsvc.get_requisition(c, rid)
        checks["confirmed dispatch = close date (jc + req)"] = (
            jc["confirmed_dispatch_date"] == today and req_after["confirmed_dispatch_date"] == today)

        # Standalone dev card (no source requisition) — INSERT must accept all-null
        # customer/dispatch fields without error.
        jc2 = await dsvc.create_dev_job_card(c, payload={
            "title": "Standalone", "lines": [{"sku_name": "Y", "qty": 1, "uom": "kg", "item_type": "rm"}]},
            user=user)
        checks["standalone jc (null customer fields)"] = (
            jc2["company_name"] is None and jc2["customer_name"] is None)

        for k, v in checks.items():
            if not v:
                fails.append(k)
        print("req id:", rid, "jc id:", jid)
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()

    post_req = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    post_jc = await c.fetchval("SELECT COUNT(*) FROM npd_dev_job_cards")
    if post_req != pre_req or post_jc != pre_jc:
        fails.append(f"LEAK req {pre_req}->{post_req}, jc {pre_jc}->{post_jc}")
    await c.close()
    print(f"counts restored: req {pre_req}=={post_req}, jc {pre_jc}=={post_jc}")
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)
    print("PASS")


asyncio.run(main())
