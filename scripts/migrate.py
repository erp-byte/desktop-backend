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
    # 039 repairs the missing uq_jcmc_v2_jc_material unique index that
    # job_card_v2.upsert_consumption_lines' ON CONFLICT relies on (declared in
    # the orphaned 018_jc_accounting_v2.sql). De-dupes then creates the index;
    # to_regclass-guarded so it no-ops if the table isn't present yet.
    DB_DIR / "039_fix_jcmc_unique_index.sql",
    # 040 repairs the missing uq_byproducts_jc_cat_mat expression index that
    # jc_accounting_v2.save_byproducts' ON CONFLICT relies on (declared in the
    # orphaned 034_byproducts_material_attribution.sql). De-dupes, drops the
    # stale 2-col UNIQUE, then creates the index; guarded like 039.
    DB_DIR / "040_fix_byproducts_unique_index.sql",
    # 041 reconciles the remaining unique indexes declared in runner-orphaned
    # migrations (011/017/021/029/032_b11/005): idx_bom_override_v2_unique
    # (ON CONFLICT-backed, de-duped keep-latest) + the integrity partial
    # indexes (created only when data is clean, else NOTICE + skip). Guarded.
    DB_DIR / "041_reconcile_orphaned_unique_indexes.sql",
    # 044 swaps consumption + byproducts + balance-material UNIQUE indexes
    # to the batch-aware shape the multi-batch app code targets
    # (upsert_consumption_lines / save_byproducts use
    # ON CONFLICT … COALESCE(batch_id, 0) …). 039 + 040 re-created the
    # OLD 2-col indexes by mistake; the new ones never landed because
    # 038_jc_batch_per_record.sql was never wired in. Idempotent + guarded.
    DB_DIR / "044_batch_aware_consumption_byproducts_indexes.sql",
    # 045 extends the job_card_batch_v2 view to expose the Stage-2
    # batch-summary columns (process_loss_kg, fg_actual_kg, fg_actual_units,
    # control_sample_kg, is_balanced, balance_difference_qty,
    # closure_remarks, input_qty_kg) that 038_jc_batch_per_record.sql
    # would have added — but that migration was never wired in. Without
    # this, GET /batches returns null for those columns even though the
    # underlying job_card_phase_v2 row holds the values, so the JC form
    # re-opens with empty Process Loss / FG Actual on closed batches.
    # Idempotent (DROP + CREATE OR REPLACE).
    DB_DIR / "045_extend_batch_view.sql",
    # 046 backfills job_card_output_v2.batch_id on historical rows.
    # record_output never accepted batch_id, so every POST /outputs save
    # left the column NULL even though sibling tables (consumption /
    # byproducts / balance) carried the correct batch_id. The frontend's
    # batchScopedDefaults fallback filters output rows by batch_id, so
    # null-tagged outputs were always skipped → FG Actual / Process Loss
    # looked blank after every reload. Idempotent — maps each null output
    # to the JC's batch that was open at its recorded_at.
    DB_DIR / "046_backfill_output_batch_id.sql",
    # 047 adds 'wastage' to job_card_byproducts_v2.category CHECK list.
    # The frontend Off-Grade dropdown exposed Wastage with a comment
    # claiming it was already a server-side bucket, but neither the
    # Python VALID_BP_CATEGORIES nor the DB CHECK declared it — every
    # save with a Wastage row 400'd invalid_category. Idempotent (drops
    # + re-adds the CHECK with the extended list).
    DB_DIR / "047_byproducts_wastage_category.sql",
    # 040 (samples/) renames entity -> warehouse on sample_requisitions /
    # gate_passes (new CHECK = warehouse codes) + transporter_name + vehicle_number.
    DB_DIR / "samples" / "040_sample_warehouse.sql",
    # 041 (samples/) adds the STANDALONE NPD development job cards
    # (npd_dev_job_cards + _lines): pure R&D, decoupled from sample requisitions;
    # closing promotes the trial recipe into a live bom_header + bom_line.
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
    # 054 adds the ON_HOLD requisition status + HOLD approval action for the NPD
    # team's review (approve / reject / hold with a reason).
    DB_DIR / "samples" / "054_npd_review_hold.sql",
    # 055 converts request_id from the generated 10000000+id column to a plain
    # BIGINT (UNIQUE) so the app can supply a time-based id (new_short_time_id).
    DB_DIR / "samples" / "055_requisition_request_id_timeid.sql",
    # 056 soft-links a request to the dev job card created from its "Develop"
    # button (source_requisition_id on the card, linked_dev_jc_id on the request).
    DB_DIR / "samples" / "056_dev_jc_requisition_link.sql",
    # 057 moves the PRIMARY KEY of sample_requisitions from the SERIAL `id` to
    # `request_id` (the surfaced 8-digit identifier). `id` is kept as a UNIQUE
    # column so the 10 inbound FKs (job_card, material_document, etc.) are
    # untouched. Drops/recreates those FKs around the PK swap; idempotent.
    DB_DIR / "samples" / "057_requisition_request_id_pkey.sql",
    # 058 adds a free-text `description` column to sample_requisitions for the
    # NPD sample-requisition form. Additive + idempotent.
    DB_DIR / "samples" / "058_requisition_description.sql",
    # 059 adds a `hold_start_date` column — set when the NPD reviewer holds a
    # request (alongside the reason). Additive + idempotent.
    DB_DIR / "samples" / "059_requisition_hold_start_date.sql",
    # 060 makes the NPD development job-card id an 8-digit time-based BIGINT
    # (new_short_time_id), widening the lines FK to match. Idempotent.
    DB_DIR / "samples" / "060_dev_jc_bigint_id.sql",
    # 061 adds npd_dev_job_card_phases — phase-wise start/complete tracking for
    # multi-day trials. Additive + idempotent.
    DB_DIR / "samples" / "061_dev_jc_phases.sql",
    # 062 adds per-phase output + material accounting columns. Additive + idempotent.
    DB_DIR / "samples" / "062_dev_jc_phase_accounting.sql",
    # 063 adds npd_dev_job_card_lines.phase_id — the trial recipe is now stored
    # per phase (independent recipe per trial iteration). Additive + idempotent.
    DB_DIR / "samples" / "063_dev_jc_phase_recipe.sql",
    # 064 adds customer + dispatch-planning fields to sample_requisitions and the
    # npd_dev_job_cards it spawns (company/customer + expected/confirmed dispatch).
    DB_DIR / "samples" / "064_requisition_customer_dispatch.sql",
    # 065 adds wa_pending_action — inbound-WhatsApp state for capturing an NPD
    # hold reason from the reviewer's reply. Additive + idempotent.
    DB_DIR / "samples" / "065_wa_pending_action.sql",
    # 066 adds pcs + weight_per_piece (quantity = pcs × weight) to the requisition
    # and the dev job card. Additive + idempotent.
    DB_DIR / "samples" / "066_requisition_pcs_weight.sql",
    # 067 maps an outbound NPD review/updated template message (Meta wamid) back to
    # its requisition, so a reviewer's Accept/Hold quick-reply button tap resolves
    # the request via the button reply's context.id. Additive + idempotent.
    DB_DIR / "samples" / "067_wa_review_message.sql",
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
