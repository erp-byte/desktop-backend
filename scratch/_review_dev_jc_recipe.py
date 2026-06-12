"""Per-phase recipe + accounting + promote-on-close (rollback-verified).

Sets up the phase + per-phase columns in-txn, then exercises the real service:
  - create with a base recipe; add_phase clones it
  - replace_phase_lines (reformulate a phase)
  - add_phase clones the latest phase's (edited) recipe
  - complete_phase records output + accounting (+ derived yield)
  - close promotes the operator-chosen phase's recipe into a live BOM
Rolls back so nothing persists."""
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
        await c.execute("ALTER TABLE npd_dev_job_card_lines ADD COLUMN IF NOT EXISTS phase_id BIGINT")
        await c.execute(
            """CREATE TABLE IF NOT EXISTS npd_dev_job_card_phases (
                 phase_id BIGINT PRIMARY KEY,
                 dev_jc_id INT NOT NULL,
                 phase_number INT NOT NULL,
                 name TEXT NOT NULL,
                 status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','IN_PROGRESS','COMPLETED')),
                 started_at TIMESTAMPTZ, started_by INT,
                 completed_at TIMESTAMPTZ, completed_by INT,
                 notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                 output_qty NUMERIC(15,3), output_uom TEXT, rm_consumed_qty NUMERIC(15,3),
                 wastage_qty NUMERIC(15,3), extra_give_away_qty NUMERIC(15,3), yield_pct NUMERIC(8,3),
                 UNIQUE (dev_jc_id, phase_number))""")
        # If the phases table already existed (migration 061) without the 062
        # accounting columns, add them in-txn so this harness is self-contained.
        for col in ("output_qty NUMERIC(15,3)", "output_uom TEXT", "rm_consumed_qty NUMERIC(15,3)",
                    "wastage_qty NUMERIC(15,3)", "extra_give_away_qty NUMERIC(15,3)", "yield_pct NUMERIC(8,3)"):
            await c.execute(f"ALTER TABLE npd_dev_job_card_phases ADD COLUMN IF NOT EXISTS {col}")
        # 064/066 columns — create_dev_job_card now INSERTs them.
        for col in ("company_name TEXT", "customer_name TEXT", "customer_contact TEXT",
                    "customer_ship_to_address TEXT", "mode_of_transport TEXT",
                    "expected_dispatch_date DATE", "confirmed_dispatch_date DATE",
                    "pcs NUMERIC(15,3)", "weight_per_piece NUMERIC(15,4)"):
            await c.execute(f"ALTER TABLE npd_dev_job_cards ADD COLUMN IF NOT EXISTS {col}")

        jc = await svc.create_dev_job_card(c, payload={
            "title": "Recipe Phase Harness",
            "lines": [
                {"sku_name": "Cashew", "qty": 60, "uom": "kg", "item_type": "rm"},
                {"sku_name": "Salt", "qty": 2, "uom": "kg", "item_type": "rm"},
            ],
        }, user=user)
        jid = jc["id"]
        checks["base recipe 2 lines"] = len(jc["lines"]) == 2

        await svc.start_development(c, jid, user=user)

        # Phase 1 clones the base recipe
        jc = await svc.add_phase(c, jid, name="Trial 1", user=user)
        p1 = jc["phases"][0]
        p1_id = p1["phase_id"]
        checks["phase1 cloned base (2 lines)"] = (
            len(p1["lines"]) == 2 and {l["sku_name"] for l in p1["lines"]} == {"Cashew", "Salt"})

        # Reformulate phase 1
        jc = await svc.replace_phase_lines(c, jid, p1_id, lines=[
            {"sku_name": "Cashew", "qty": 58, "uom": "kg", "item_type": "rm"},
            {"sku_name": "Salt", "qty": 1.5, "uom": "kg", "item_type": "rm"},
            {"sku_name": "Pepper", "qty": 0.5, "uom": "kg", "item_type": "rm"},
        ], user=user)
        p1 = next(p for p in jc["phases"] if p["phase_id"] == p1_id)
        checks["phase1 reformulated (3 lines)"] = len(p1["lines"]) == 3

        # Start + complete phase 1 with output + accounting
        await svc.start_phase(c, jid, p1_id, user=user)
        jc = await svc.complete_phase(c, jid, p1_id, payload={
            "output_qty": 50, "output_uom": "kg", "rm_consumed_qty": 60,
            "wastage_qty": 8, "extra_give_away_qty": 2, "notes": "ran 3 days"}, user=user)
        p1 = next(p for p in jc["phases"] if p["phase_id"] == p1_id)
        checks["phase1 completed + accounting + yield"] = (
            p1["status"] == "COMPLETED" and float(p1["output_qty"]) == 50
            and float(p1["rm_consumed_qty"]) == 60
            and float(p1["yield_pct"]) == round(50 / 60 * 100, 2))

        # Phase 2 clones phase 1's edited recipe (latest phase)
        jc = await svc.add_phase(c, jid, name="Trial 2", user=user)
        p2 = next(p for p in jc["phases"] if p["phase_number"] == 2)
        checks["phase2 cloned phase1 (3 lines)"] = (
            len(p2["lines"]) == 3 and {l["sku_name"] for l in p2["lines"]} == {"Cashew", "Salt", "Pepper"})

        # Ordering: open phase (Trial 2 PENDING) at top, completed (Trial 1) at bottom.
        checks["open phase before completed"] = (
            jc["phases"][0]["status"] != "COMPLETED" and jc["phases"][-1]["status"] == "COMPLETED"
            and jc["phases"][0]["phase_number"] == 2)

        # Delete phase 2 — it and its recipe lines (FK cascade) are removed.
        p2_id = p2["phase_id"]
        jc = await svc.delete_phase(c, jid, p2_id, user=user)
        lines_left = await c.fetchval(
            "SELECT COUNT(*) FROM npd_dev_job_card_lines WHERE dev_jc_id=$1 AND phase_id=$2", jid, p2_id)
        checks["delete phase removes phase + its lines"] = (
            all(p["phase_id"] != p2_id for p in jc["phases"]) and lines_left == 0)

        # Base recipe is editable while IN_DEVELOPMENT (does not touch phase lines).
        jc = await svc.replace_lines(c, jid, lines=[
            {"sku_name": "Cashew", "qty": 70, "uom": "kg", "item_type": "rm"},
        ], user=user)
        checks["base editable in IN_DEVELOPMENT"] = (
            len(jc["lines"]) == 1 and float(jc["lines"][0]["qty"]) == 70
            # phase recipes untouched by the base edit
            and len(next(p for p in jc["phases"] if p["phase_id"] == p1_id)["lines"]) == 3)

        # Close promoting phase 1 — its recorded output/accounting is INHERITED
        # (no card-level accounting in the payload), and FG receipt fires (out > 0).
        jc = await svc.close_dev_job_card(c, jid, payload={"promote_phase_id": p1_id}, user=user)
        checks["closed + promoted_bom set"] = jc["status"] == "CLOSED" and jc["promoted_bom_id"] is not None
        checks["close inherits phase accounting"] = (
            float(jc["output_qty"]) == 50 and float(jc["rm_consumed_qty"]) == 60
            and float(jc["wastage_qty"]) == 8 and float(jc["yield_pct"]) == 83.33
            and jc["fg_sample_batch_id"] is not None)
        bom_names = {r["material_sku_name"] for r in await c.fetch(
            "SELECT material_sku_name FROM bom_line WHERE bom_id = $1", jc["promoted_bom_id"])}
        checks["promoted BOM = phase1 recipe"] = bom_names == {"Cashew", "Salt", "Pepper"}

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
