"""Standalone DB migration runner.

Run before each Lambda deploy to apply schema changes:

    python scripts/migrate.py

Reads DATABASE_URL from environment or .env file.
All SQL files are idempotent (IF NOT EXISTS / ON CONFLICT).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).parent.parent / "app" / "db"

# Applied in dependency order — schemas before their migrations
SQL_FILES = [
    DB_DIR / "schema.sql",
    DB_DIR / "migrate.sql",
    DB_DIR / "po_schema.sql",
    DB_DIR / "po_migrate.sql",
    DB_DIR / "production_schema.sql",
    DB_DIR / "production_migrate.sql",
    DB_DIR / "auth_schema.sql",
    DB_DIR / "ims_new_schema.sql",
    DB_DIR / "sap_mm_align.sql",
    DB_DIR / "001_job_card_chain.sql",
    DB_DIR / "030_vendor_history.sql",
    DB_DIR / "031_bom_bar_line_process.sql",
    DB_DIR / "seed_test_data.sql",
    # 032 backfills uom_match for SO Book uploads that landed before the
    # reconciliation switched from string-UOM equality to the kg/pack-count
    # math check. Idempotent — only touches rows where uom_match IS NULL.
    DB_DIR / "032_so_uom_recon_backfill.sql",
    # 033 stages OTPs for the WhatsApp-based self-service password reset
    # that replaced the admin-only reset path. One row per user (PK), 60s
    # TTL, deleted on successful reset.
    DB_DIR / "033_password_reset_otp.sql",
    # 034 creates the QC Inward Inspection tables (qc_intimation, qc_parameter,
    # qc_sku_spec, qc_reading, qc_inspection_audit) and extends the existing
    # qc_inspection table (from ims_new_schema.sql) with inward-inspection
    # columns via ADD COLUMN IF NOT EXISTS. Seeds 3 qc_parameter rows, spec
    # bands for sku_id=1, and 3 pending qc_intimation rows.
    DB_DIR / "034_qc_inspection.sql",
    # 035_qc_intimation_invoice adds invoice_no TEXT column to qc_intimation
    # so Material In "Send intimation" can persist the invoice reference
    # alongside each arrival event row. Idempotent ADD COLUMN IF NOT EXISTS.
    DB_DIR / "035_qc_intimation_invoice.sql",
    # 036 extends qc_parameter into the full RM-check parameter catalog
    # (code/param_group/data_type/value_kind/spec_note/sort_order) and seeds the
    # 34 measurable parameters from candor_rm_ncr_fields.xlsx ("RM Check
    # Parameters" sheet). Reconciles the 3 legacy 034 seed rows to canonical
    # codes. Idempotent (ON CONFLICT (code) DO UPDATE).
    DB_DIR / "036_qc_param_catalog.sql",
    # 037 creates the single ncr_record table (NCR Report Fields sheet) with the
    # failed-parameter and supplier-CAPA 1:N sets folded into JSONB columns —
    # minimum-tables design. App-supplied 8-digit BIGINT PK.
    DB_DIR / "037_ncr.sql",
    # 038 adds approved_by / approved_by_name / approved_at to qc_inward_inspection
    # — the manager's verdict sign-off, surfaced as an Approval record.
    DB_DIR / "038_qc_inspection_approval.sql",
    # ── Sample Issuing module (app/db/samples/) ────────────────────────
    # 035 adds the business_head / npd_team roles + the `sample` permission
    # catalog (inventory_manager already exists from 028).
    DB_DIR / "samples" / "035_sample_roles.sql",
    # 036 builds the GENERIC gate_passes table + registers sample movement
    # types 265/266. Must run before 037 (sample tables FK gate_passes).
    DB_DIR / "samples" / "036_gate_passes.sql",
    # 037 creates the sample_* tables, seeds the approval role-map with real
    # roles, and extends material_document. Depends on 035 + 036.
    DB_DIR / "samples" / "037_sample_module.sql",
    # 038 extends the legacy job_card table (sample_requisition_id,
    # jobcard_type) and adds the sample_requisitions.linked_job_card_id FK.
    DB_DIR / "samples" / "038_job_card_sample_fk.sql",
    # 040 renames entity -> warehouse on sample_requisitions / gate_passes
    # (new CHECK = warehouse codes) and adds transporter_name + vehicle_number.
    DB_DIR / "samples" / "040_sample_warehouse.sql",
    # 041 adds the STANDALONE NPD development job cards (npd_dev_job_cards +
    # _lines): pure R&D, decoupled from sample requisitions; closing promotes
    # the trial recipe into a live bom_header + bom_line.
    DB_DIR / "samples" / "041_npd_dev_job_cards.sql",
    # 042 adds the TRIAL sample type (Customer Trials, §3) — extends the
    # sample_type + jobcard_type CHECKs and seeds the TRIAL production-ack row.
    DB_DIR / "samples" / "042_trial_sample_type.sql",
    # 043 adds per-ingredient ownership (OWN/CUSTOMER) + off-master flags to both
    # NPD recipe-line tables — the accounting backbone skips inventory postings
    # for customer-supplied / off-master lines.
    DB_DIR / "samples" / "043_recipe_line_ownership.sql",
    # 044 adds the npd_authorized_users allow-list (the "2 specific people");
    # empty = role gate only, seed the two user_ids later.
    DB_DIR / "samples" / "044_npd_authorized_users.sql",
    # 045 inventory accounting backbone: fg_sample_batch_id (Step B receipt) on
    # the job-card homes + sample_consumption_variance (silent Step A variance).
    DB_DIR / "samples" / "045_sample_inventory_accounting.sql",
    # 046 dev-job-card dispatch (Section 2 Step C): record recipient/qty + the
    # 265 that issues the developed FG sample out of the R&D location.
    DB_DIR / "samples" / "046_dev_jc_dispatch.sql",
    # 047 RM Issue / Collection Form (Document 015): maker-checker indent whose
    # Store-issue action fires Step A (265). DRAFT->SUBMITTED->APPROVED->ISSUED.
    DB_DIR / "samples" / "047_rm_issue_form.sql",
    # 048 notification/maker-checker matrix: routes each event -> teams + email
    # (SMTP via mail_service, no-op until configured). Seeds RM-form + gate rows.
    DB_DIR / "samples" / "048_notification_matrix.sql",
    # 049 repair: restore lost SERIAL id defaults on the 036/037 sample tables
    # (id sequences were dropped in some envs -> NOT NULL violation on insert).
    DB_DIR / "samples" / "049_restore_sample_id_defaults.sql",
    # 050 closure accounting on dev job cards (rm_consumed/wastage/extra_give_away)
    # — the material-balance summary + auto yield % at close.
    DB_DIR / "samples" / "050_dev_jc_accounting.sql",
    # 051 captures the requested NPD target article name ON the requisition so a
    # business head can name the new product when raising the request.
    DB_DIR / "samples" / "051_requisition_npd_target.sql",
    # 052 adds an 8-digit BIGINT request_id per requisition (generated 10000000+id)
    # — a short numeric handle alongside the SMP-YYYYMMDD-NNNN number.
    DB_DIR / "samples" / "052_requisition_request_id.sql",
    # 053 adds a nullable float `quantity` the requester can put on the request.
    DB_DIR / "samples" / "053_requisition_quantity.sql",
]


async def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable not set")
        sys.exit(1)

    logger.info("Connecting to database...")
    conn = await asyncpg.connect(database_url)
    try:
        for sql_file in SQL_FILES:
            if not sql_file.exists():
                logger.warning("Skipping missing file: %s", sql_file.name)
                continue
            logger.info("Applying %s ...", sql_file.name)
            sql = sql_file.read_text(encoding="utf-8")
            await conn.execute(sql)
            logger.info("  OK")
        logger.info("All migrations applied successfully.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
