"""Seed PO 896 / job card 896/1 for SO CF-SO/26-27/130 (PL Sliced Cranberries 100G)
with the actuals from the printed job card image, then generate the PDF.

Idempotent: if prod_order_number='896' already exists it does NOT insert duplicates.
All DB writes happen inside ONE transaction.

Output: tmp/job-card-PO-896.pdf
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg

from app.modules.production.services.job_card_pdf import generate_job_card_pdf

DB_URL = "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db"
OUT_PDF = Path(r"D:\Consumption\New\Backend\tmp\job-card-PO-896.pdf")

# ── values from the printed form ─────────────────────────────────────────
PO_NUMBER       = "896"
JC_NUMBER       = "896/1"
SO_REF          = "CF-SO/26-27/130"
BATCH_NUMBER    = "JD27"
LOT_NUMBER      = "183833"

FG_SKU          = "PL SLICED CRANBERRIES 100G"
CUSTOMER        = "Candor Foods Pvt Ltd (Internal SO)"
BOM_ID          = 824
ENTITY          = "cfpl"
FACTORY         = "W202"
FLOOR           = "Lower Basement"
BU              = "RPC"
EAN             = "494408753"
MRP             = 140

PLANNED_UNITS   = 6100
PLANNED_KG      = 616.1
PACK_KG         = 0.100
SHELF_LIFE_DAYS = 365

# date-of-production from form: 24-Apr-26
DOP             = date(2026, 4, 24)
START_TS        = datetime(2026, 4, 24,  9, 10, tzinfo=timezone.utc)
END_TS          = datetime(2026, 4, 24, 17, 50, tzinfo=timezone.utc)
BEST_BEFORE     = date(2027, 4, 24)        # +365d

TEAM_LEADER     = "Namrata"
TEAM_MEMBERS    = ["Parvati", "Manisha", "Sabina"]

CRANBERRY_REQD_KG  = 616.1
CRANBERRY_ISSUED_KG = 616.0
POUCH_REQD_NOS  = 6100
POUCH_ISSUED_NOS = 6100
CARTON_REQD_NOS = 61
CARTON_ISSUED_NOS = 61

FG_ACTUAL_UNITS = 6090
FG_ACTUAL_KG    = round(FG_ACTUAL_UNITS * PACK_KG, 3)   # 609.0
RM_CONSUMED_KG  = 616.0
PROCESS_LOSS_KG = 6.9
CONTROL_SAMPLE_GM = 100

# PM SKU names from bom_line (preserve exactly so future joins line up)
POUCH_SKU  = "PM24-Cranberries Pouch (Reliance-no brand) 100 gmPM24-Cranberries Pouch (Reliance-unbranded) 100 gm"
CARTON_SKU = "PM24-3 Ply Carton CF21 Walnut Carton (445X310X305MM)-Unprinted"


async def main():
    conn = await asyncpg.connect(DB_URL)
    try:
        # Already exists?
        existing = await conn.fetchrow(
            "SELECT prod_order_id, status FROM production_order WHERE prod_order_number = $1",
            PO_NUMBER,
        )
        if existing:
            print(f"WARN: PO {PO_NUMBER} already exists: prod_order_id={existing['prod_order_id']} "
                  f"status={existing['status']}. Skipping inserts; will only generate PDF.")
            prod_order_id = existing['prod_order_id']
            jc_row = await conn.fetchrow(
                "SELECT job_card_id FROM job_card WHERE prod_order_id = $1 ORDER BY step_number LIMIT 1",
                prod_order_id,
            )
            if not jc_row:
                raise SystemExit(f"PO {PO_NUMBER} exists but no job_card found. Aborting.")
            job_card_id = jc_row['job_card_id']
        else:
            async with conn.transaction():
                # 1) production_order
                prod_order_id = await conn.fetchval(
                    """
                    INSERT INTO production_order (
                        prod_order_number, plan_line_id, bom_id, fg_sku_name, customer_name,
                        batch_number, batch_size_kg, net_wt_per_unit, best_before,
                        total_stages, entity, factory, floor, status, created_at
                    ) VALUES ($1, NULL, $2, $3, $4, $5, $6, $7, $8, 1, $9, $10, $11, 'completed', $12)
                    RETURNING prod_order_id
                    """,
                    PO_NUMBER, BOM_ID, FG_SKU, CUSTOMER,
                    BATCH_NUMBER, PLANNED_KG, PACK_KG, BEST_BEFORE,
                    ENTITY, FACTORY, FLOOR, datetime.combine(DOP, datetime.min.time(), tzinfo=timezone.utc),
                )

                # 2) job_card (single packaging stage)
                job_card_id = await conn.fetchval(
                    """
                    INSERT INTO job_card (
                        job_card_number, prod_order_id, bom_id, step_number, process_name, stage,
                        fg_sku_name, customer_name, batch_number, batch_size_kg,
                        is_locked, locked_reason, force_unlocked,
                        status, start_time, end_time, total_time_min,
                        factory, floor, entity,
                        sales_order_ref, mrp, ean, bu,
                        fumigation, metal_detector_used, roasting_pasteurization, magnets_used,
                        control_sample_gm, store_allocation_status,
                        assigned_to_team_leader, team_members, created_at
                    ) VALUES (
                        $1, $2, $3, 1, 'Packaging', 'packaging',
                        $4, $5, $6, $7,
                        FALSE, NULL, TRUE,
                        'completed', $8, $9, $10,
                        $11, $12, $13,
                        $14, $15, $16, $17,
                        FALSE, FALSE, FALSE, FALSE,
                        $18, 'approved',
                        $19, $20, $21
                    ) RETURNING job_card_id
                    """,
                    JC_NUMBER, prod_order_id, BOM_ID,
                    FG_SKU, CUSTOMER, BATCH_NUMBER, PLANNED_KG,
                    START_TS, END_TS, (END_TS - START_TS).total_seconds() / 60.0,
                    FACTORY, FLOOR, ENTITY,
                    SO_REF, MRP, EAN, BU,
                    CONTROL_SAMPLE_GM,
                    TEAM_LEADER, TEAM_MEMBERS,
                    datetime.combine(DOP, datetime.min.time(), tzinfo=timezone.utc),
                )

                # 3) RM indent — Dried Cranberry Sliced
                cb_gross = round(CRANBERRY_REQD_KG / (1 - 0.02), 3)
                await conn.execute(
                    """
                    INSERT INTO job_card_rm_indent (
                        job_card_id, material_sku_name, uom, reqd_qty, loss_pct,
                        gross_qty, issued_qty, batch_no, godown,
                        variance, status, created_at
                    ) VALUES ($1, $2, 'Kg', $3, 2.000, $4, $5, $6, 'Factory',
                              $7, 'fulfilled', $8)
                    """,
                    job_card_id, "Dried Cranberry Sliced",
                    CRANBERRY_REQD_KG, cb_gross, CRANBERRY_ISSUED_KG, LOT_NUMBER,
                    round(CRANBERRY_ISSUED_KG - cb_gross, 3),
                    datetime.combine(DOP, datetime.min.time(), tzinfo=timezone.utc),
                )

                # 4) PM indents — pouches + cartons
                pouch_gross = round(POUCH_REQD_NOS / (1 - 0.01), 3)
                carton_gross = round(CARTON_REQD_NOS / (1 - 0.01), 3)
                await conn.execute(
                    """
                    INSERT INTO job_card_pm_indent (
                        job_card_id, material_sku_name, uom, reqd_qty, loss_pct,
                        gross_qty, issued_qty, godown, status, created_at
                    ) VALUES ($1, $2, 'Pcs', $3, 1.000, $4, $5, 'PM Store', 'fulfilled', $6)
                    """,
                    job_card_id, POUCH_SKU,
                    POUCH_REQD_NOS, pouch_gross, POUCH_ISSUED_NOS,
                    datetime.combine(DOP, datetime.min.time(), tzinfo=timezone.utc),
                )
                await conn.execute(
                    """
                    INSERT INTO job_card_pm_indent (
                        job_card_id, material_sku_name, uom, reqd_qty, loss_pct,
                        gross_qty, issued_qty, godown, status, created_at
                    ) VALUES ($1, $2, 'Pcs', $3, 1.000, $4, $5, 'PM Store', 'fulfilled', $6)
                    """,
                    job_card_id, CARTON_SKU,
                    CARTON_REQD_NOS, carton_gross, CARTON_ISSUED_NOS,
                    datetime.combine(DOP, datetime.min.time(), tzinfo=timezone.utc),
                )

                # 5) job_card_output — FG actuals + loss
                yield_pct = round(FG_ACTUAL_KG / RM_CONSUMED_KG * 100, 3)
                await conn.execute(
                    """
                    INSERT INTO job_card_output (
                        job_card_id, fg_expected_units, fg_actual_units,
                        fg_expected_kg, fg_actual_kg, rm_consumed_kg,
                        process_loss_kg, net_output_kg, yield_pct, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    job_card_id, PLANNED_UNITS, FG_ACTUAL_UNITS,
                    round(PLANNED_UNITS * PACK_KG, 3), FG_ACTUAL_KG, RM_CONSUMED_KG,
                    PROCESS_LOSS_KG, FG_ACTUAL_KG, yield_pct,
                    datetime.combine(DOP, datetime.min.time(), tzinfo=timezone.utc),
                )

                # 6) one process step row for completeness (matches engine)
                await conn.execute(
                    """
                    INSERT INTO job_card_process_step (
                        job_card_id, step_number, process_name, machine_name,
                        std_time_min, qc_check, loss_pct, status, created_at
                    ) VALUES ($1, 1, 'Packaging', NULL, NULL, NULL, 1.0, 'completed', $2)
                    """,
                    job_card_id,
                    datetime.combine(DOP, datetime.min.time(), tzinfo=timezone.utc),
                )

                print(f"OK: Inserted PO {PO_NUMBER} (prod_order_id={prod_order_id}) + "
                      f"JC {JC_NUMBER} (job_card_id={job_card_id}) + RM/PM/output rows.")

        # ─── Build PDF dict and generate ───────────────────────────────────
        # Re-fetch to confirm everything is there and to drive the PDF off DB state.
        jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", job_card_id)
        po = await conn.fetchrow("SELECT * FROM production_order WHERE prod_order_id = $1", prod_order_id)
        rm = await conn.fetch("SELECT * FROM job_card_rm_indent WHERE job_card_id = $1 ORDER BY rm_indent_id", job_card_id)
        pm = await conn.fetch("SELECT * FROM job_card_pm_indent WHERE job_card_id = $1 ORDER BY pm_indent_id", job_card_id)
        out = await conn.fetchrow("SELECT * FROM job_card_output WHERE job_card_id = $1", job_card_id)

        # Merge RM + PM into the section_2a_rm_indent list the PDF generator reads
        rm_section = []
        for r in rm:
            rm_section.append({
                "material_sku_name": r["material_sku_name"],
                "reqd_qty": float(r["reqd_qty"]),
                "issued_qty": float(r["issued_qty"]) if r["issued_qty"] is not None else None,
                "batch_no": r["batch_no"] or LOT_NUMBER,
                "uom": r["uom"] or "Kg",
            })
        for r in pm:
            rm_section.append({
                "material_sku_name": r["material_sku_name"],
                "reqd_qty": float(r["reqd_qty"]),
                "issued_qty": float(r["issued_qty"]) if r["issued_qty"] is not None else None,
                "batch_no": r["batch_no"] or "",
                "uom": r["uom"] or "Pcs",
            })

        material_consumption = [
            {"material_sku_name": "Dried Cranberry Sliced", "actual_consumed_qty": RM_CONSUMED_KG},
            {"material_sku_name": POUCH_SKU,  "actual_consumed_qty": POUCH_ISSUED_NOS},
            {"material_sku_name": CARTON_SKU, "actual_consumed_qty": CARTON_ISSUED_NOS},
        ]

        section_5_output = {
            "fg_actual_units": int(out["fg_actual_units"]),
            "fg_actual_kg":    float(out["fg_actual_kg"]),
            "rm_consumed_kg":  float(out["rm_consumed_kg"]),
            "net_output_kg":   float(out["net_output_kg"]),
            "yield_pct":       float(out["yield_pct"]),
        }

        material_accounting = {
            "process_loss_kg":           float(out["process_loss_kg"]),
            "process_loss_pct":          round(float(out["process_loss_kg"]) / float(out["rm_consumed_kg"]) * 100, 3),
            "extra_give_away_kg":        None,
            "balance_material_kg":       None,
            "control_sample_kg":         CONTROL_SAMPLE_GM / 1000.0,
            "wastage_kg":                None,
            "total_material_issued_kg":  CRANBERRY_ISSUED_KG,
            "total_loss_pct":            round(float(out["process_loss_kg"]) / float(out["rm_consumed_kg"]) * 100, 3),
            "offgrade_total_kg":         0.0,
        }

        section_3_team = {
            "team_leader":             jc["assigned_to_team_leader"],
            "team_members":            list(jc["team_members"] or []),
            "start_time":              jc["start_time"].strftime("%H:%M") if jc["start_time"] else "--",
            "end_time":                jc["end_time"].strftime("%H:%M") if jc["end_time"] else "--",
            "fumigation":              bool(jc["fumigation"]),
            "metal_detector_used":     bool(jc["metal_detector_used"]),
            "roasting_pasteurization": bool(jc["roasting_pasteurization"]),
            "control_sample_gm":       float(jc["control_sample_gm"]) if jc["control_sample_gm"] is not None else None,
            "magnets_used":            bool(jc["magnets_used"]),
        }

        section_1_product = {
            "customer_name":   jc["customer_name"],
            "fg_sku_name":     jc["fg_sku_name"],
            "batch_number":    jc["batch_number"],
            "article_code":    jc["article_code"] or "--",
            "batch_size_kg":   float(jc["batch_size_kg"]),
            "quantity_units":  PLANNED_UNITS,
            "mrp":             float(jc["mrp"]) if jc["mrp"] is not None else "--",
            "ean":             jc["ean"] or "--",
            "best_before":     str(po["best_before"]) if po["best_before"] else "--",
            "factory":         jc["factory"] or "--",
            "floor":           jc["floor"] or "--",
            "shelf_life_days": SHELF_LIFE_DAYS,
            "sales_order_ref": jc["sales_order_ref"],
        }

        jc_data = {
            "job_card_number":       jc["job_card_number"],
            "created_at":            jc["created_at"].strftime("%Y-%m-%d"),
            "section_1_product":     section_1_product,
            "section_2a_rm_indent":  rm_section,
            "section_3_team":        section_3_team,
            "section_5_output":      section_5_output,
            "material_consumption":  material_consumption,
            "material_accounting":   material_accounting,
            "section_6_signoffs": {
                "production_manager": {"name": "Production Incharge"},
                "qc_inspector":       {"name": "QC"},
                "floor_incharge":     {"name": ""},
            },
        }

        OUT_PDF.parent.mkdir(parents=True, exist_ok=True)
        pdf_bytes = generate_job_card_pdf(jc_data, mode="full")
        OUT_PDF.write_bytes(pdf_bytes)
        print(f"OK: PDF written: {OUT_PDF}  ({len(pdf_bytes):,} bytes)")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
