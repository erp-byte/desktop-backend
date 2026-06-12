"""Close-promotion BOM versioning (rollback-verified).

Reproduces the reported 500: an active bom_header already exists for the FG name,
then a dev card with the same FG name is closed. The fix supersedes the old active
BOM and mints the next version instead of always inserting version 1.
Rolls back so nothing persists."""
import asyncio
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.modules.sample.services import npd_dev_service as svc

load_dotenv()

FG = "ZZ_BOM_PROMOTE_TEST_ZZ"
_COLS = ("company_name TEXT", "customer_name TEXT", "customer_contact TEXT",
         "customer_ship_to_address TEXT", "mode_of_transport TEXT",
         "expected_dispatch_date DATE", "confirmed_dispatch_date DATE",
         "pcs NUMERIC(15,3)", "weight_per_piece NUMERIC(15,4)")


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
    user = types.SimpleNamespace(user_id=uid, role_name="admin", is_admin=True, full_name="R")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        for col in _COLS:
            await c.execute(f"ALTER TABLE npd_dev_job_cards ADD COLUMN IF NOT EXISTS {col}")
        await c.execute("ALTER TABLE npd_dev_job_card_lines ADD COLUMN IF NOT EXISTS phase_id BIGINT")

        # Pre-existing ACTIVE BOM v1 for the FG (the condition that 500'd before).
        old_bom = await c.fetchval(
            "INSERT INTO bom_header (fg_sku_name, version, is_active, entity) "
            "VALUES ($1, 1, TRUE, 'cfpl') RETURNING bom_id", FG)

        jc = await svc.create_dev_job_card(c, payload={
            "title": "BOM Promote", "fg_sku_name": FG,
            "lines": [{"sku_name": "A", "qty": 1, "uom": "kg", "item_type": "rm"}]}, user=user)
        jid = jc["id"]
        await svc.start_development(c, jid, user=user)
        jc = await svc.close_dev_job_card(c, jid, payload={}, user=user)  # promotes base recipe

        new_bom = jc["promoted_bom_id"]
        new_ver = await c.fetchval("SELECT version FROM bom_header WHERE bom_id = $1", new_bom)
        new_active = await c.fetchval("SELECT is_active FROM bom_header WHERE bom_id = $1", new_bom)
        old_active = await c.fetchval("SELECT is_active FROM bom_header WHERE bom_id = $1", old_bom)
        active_count = await c.fetchval(
            "SELECT COUNT(*) FROM bom_header WHERE fg_sku_name = $1 AND is_active = TRUE", FG)
        checks["close succeeds (no 500) + promoted_bom set"] = new_bom is not None and jc["status"] == "CLOSED"
        checks["new BOM minted as version 2"] = new_ver == 2 and new_active is True
        checks["old v1 superseded (inactive)"] = old_active is False
        checks["exactly one active BOM for the FG"] = active_count == 1

        for k, v in checks.items():
            if not v:
                fails.append(k)
        print("old_bom:", old_bom, "new_bom:", new_bom, "new_ver:", new_ver)
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()
    await c.close()
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)
    print("PASS")


asyncio.run(main())
