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
    # 048 adds auth_user_role — a person can hold multiple roles at once
    # (admin + business_head, etc.). Join table is the source of truth for the
    # full role set; auth_user.role_id stays the primary/display role. Backfills
    # each user's current single role. See app/modules/auth for the resolution.
    DB_DIR / "048_auth_user_role.sql",
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
    # ── SFG / Job-Card integration (promoted from app/db/test_db) ───────
    # 050 SFG foundation: widen the item_type comment (no CHECK exists), add the
    # bom_process_route routing/seam columns + Option-B bom_line.consumed_at_stage,
    # and (GUARDED via to_regclass, since 017_job_card_v2 is orphaned in this runner)
    # the SFG#### chain-code columns job_card_v2.input_code/output_code. Idempotent.
    DB_DIR / "050_sfg_foundation.sql",
    # 051 SFG catalogue schema (Slice 1): widen all_sku.sku_id SERIAL->BIGINT
    # (so new SFG rows can carry an 8-digit app-supplied PK; SERIAL default kept
    # for every other insert site), add the all_sku.sfg_code business key + its
    # partial unique index. Idempotent. The 343 SFG ROWS are loaded by
    # master_ingest.ingest_sfg_master() at app startup (it mints each new row's
    # 8-digit BIGINT id via new_short_time_id() + insert_with_pk_retry()), not
    # here — a static SQL migration can't do time-id + collision retry.
    DB_DIR / "051_sfg_seed_catalog.sql",
    # 052 SFG routing (Slice 3): the 2-stage chain + SFG seam. Schema-support
    # only (seam columns already added by 050) — documentary COMMENTs pinning the
    # seam semantics; the reverse SFG#### indexes were deliberately deferred to
    # 055 (the slice that adds the actual where-used query). The 1460 routing rows
    # are loaded by master_ingest.ingest_jc_routing() at startup (needs
    # name-normalise + article->bom_id resolve), not here.
    DB_DIR / "052_sfg_routing_bom.sql",
    # 053 sfg_box: physical SFG box/bag rows for WIP-completion QR labels (mirror of
    # po_box); the box_id is the QR payload. Standalone, idempotent.
    DB_DIR / "053_sfg_box.sql",
    # 055 SFG catalogue layer: three DERIVED views (sfg_master, fg_sfg_binding,
    # sfg_where_used) that give the design-ref §8.1/§9.2 "standalone catalogue
    # artifacts" without duplicating data — projected from all_sku + the
    # bom_process_route seam — plus the reverse SFG#### partial indexes deferred
    # by 052. Reversible via app/db/rollback/sfg_integration_down.sql.
    DB_DIR / "055_sfg_catalogue_views.sql",
    # 056 SFG enrichment: backing tables for the 4 companion plug files that
    # 050-055 left unused — sfg_attributes (JC_Stage1: base_recipe/operation/origin,
    # NO shelf life), stage_catalog (ProcessCategory_to_Operation §6 config),
    # fg_sfg_input_map (JC_Stage2 reconciliation), sfg_resolution_map (dedup
    # provenance) — and re-defines sfg_master to surface base_recipe/operation.
    # Rows loaded by master_ingest at startup. Reversible via the same down script.
    DB_DIR / "056_sfg_master_enrichment.sql",
    # 057 structural tightening: inventory_batch.sfg_code (canonical WIP key, so
    # sku_name stops being overloaded with the code; existing WIP rows backfilled)
    # + sfg_attributes.va_article/primary_bu readiness for the (absent) FINAL source,
    # surfaced in a re-defined sfg_master. Reversible via the same down script.
    DB_DIR / "057_sfg_inventory_key_and_attrs.sql",
    # 058 Slice-7 data-quality: adds bom_header.promoted_from_gap — the audit
    # marker the reconciliation gap promotion stamps on rows it creates/routes
    # (the 403 CATALOG_FG_NOT_IN_FG_MASTER / 238 BOM_PARENT_NO_ROUTING articles).
    # Schema only; the actual promotion is DATA, loaded by
    # scripts/promote_fg_master_gaps.py (it needs the article list from the CSV).
    # Additive + idempotent. Reversible via app/db/rollback/sfg_integration_down.sql.
    DB_DIR / "058_fg_master_gap_promotion.sql",
    # 059 SFG genealogy (Phase 7): activates the Slice-6-DEFERRED batch/lot
    # traceability — adds the partial traversal indexes on sfg_box(parent_box_id)
    # and sfg_box(lot_number) (the received_into_job_card_id index already exists
    # from 053) + the sfg_genealogy VIEW that flattens each box to its provenance
    # (producer/downstream JC, parent box, source WIP inventory_batch + lot).
    # No new columns (053 already has them); indexes + view only. Idempotent.
    # Reversible via app/db/rollback/sfg_integration_down.sql.
    DB_DIR / "059_sfg_genealogy.sql",
    # 060 Routing-Gap status: the live fg_routing_status VIEW — one row per FG
    # bom_header with route_step_count + has_routing / needs_routing booleans
    # (derived from a LEFT JOIN to bom_process_route). The DB-side complement to
    # the offline Article_Master_FINAL.csv gap list, driving the Routing-Gap
    # Resolution feature. No new columns/indexes (bom_process_route(bom_id) is
    # already indexed); view only. Idempotent. Reversible via
    # app/db/rollback/sfg_integration_down.sql.
    DB_DIR / "060_routing_gap_status.sql",
    # 061 Bar-Line Process override (G4): adds bom_header.bar_line_routed (audit
    # marker the G4 override stamps on FGs whose bom_process_route it (re)builds
    # from the richer bar_line_process string, mig 031) + its partial index, and
    # the bar_line_fg VIEW listing the ~84 candidate FGs that carry a non-blank
    # bar_line_process. Schema/audit only; the override itself is DATA (opt-in,
    # idempotent script). Additive + idempotent. Reversible via
    # app/db/rollback/sfg_integration_down.sql.
    DB_DIR / "061_bar_line_routing.sql",
    # 062 SFG seam minting (Phase 9): the sfg_seam_pending VIEW — one row per
    # Create-WIP bom_process_route step that still lacks an SFG seam (output_code
    # NULL/blank), the work-queue the opt-in minting script targets and the
    # post-mint audit that drains to EMPTY once every transform chain is seamed,
    # plus its partial index (NULL-side of output_code, not covered by 055's
    # NOT-NULL index). Observability only; the minting is DATA (opt-in script). No
    # new columns; view + index only. Additive + idempotent. Reversible via
    # app/db/rollback/sfg_integration_down.sql.
    DB_DIR / "062_sfg_seam_pending.sql",
    # 063 Plan List: production_plan_v2.created_by — persist the creating user so
    # the Plan List + detail preview can show "Created by". Volume/units stay
    # derived. to_regclass-guarded (v2 header is applied out-of-band). Idempotent.
    DB_DIR / "063_plan_created_by.sql",
    # 064 job_card_edit_log_v2 — audit trail for live (started-chain) Edit-Job-Card
    # actions (floor change / add / qty change / remove-with-forced-snapshot) and
    # the resulting SO sync delta (ledger + so_line). Additive + idempotent.
    DB_DIR / "064_jc_edit_audit.sql",
    # 065 seed FG-dispatch recipient roles (billing, candor_operations,
    # operations_head) so the "Dispatch to" To/CC can resolve to users. Idempotent.
    DB_DIR / "065_dispatch_roles.sql",
    # 066 fg_dispatch_log_v2 — audit log for FG dispatch notifications from a
    # completed packaging stage (body fields + transport + recipients). Additive.
    DB_DIR / "066_fg_dispatch_log.sql",
    # 069 packing_details — Packing Details module. One row per packing record:
    # batch_code + article_name business handles + a free-form JSONB `details`
    # body. packing_id is an app-supplied 8-digit time-based BIGINT
    # (new_short_time_id), matching the job-card v2 PK convention. Numbered 069
    # because 067/068 already exist on disk (out-of-band SFG migrations).
    # Additive + idempotent.
    DB_DIR / "069_packing_details.sql",
    # 070 creates the Customer-Returns module tables: per-company
    # cfpl_/cdpl_customer_return_header/_lines/_boxes (natural keys, rtv_id PK)
    # plus the global box_edit_logs audit table. Idempotent CREATE IF NOT EXISTS.
    DB_DIR / "070_customer_returns.sql",
    # 071 sfg_box_string_id — carton_id (PK) + parent_box_id: BIGINT -> TEXT so a
    # box id becomes "<8-digit-time-base>-<per-JC counter>" (matches po_box / RM).
    # Drops + recreates the sfg_genealogy view (the only ALTER blocker); idempotent
    # via a bigint-guard. Assumes 067's carton_id rename (applied out-of-band).
    DB_DIR / "071_sfg_box_string_id.sql",
    # 072 batch_label — operator-typed free-text batch name on the accounting
    # "Open Batch" flow. Adds job_card_phase_v2.batch_label TEXT and recreates
    # the job_card_batch_v2 view to expose it; batch_number stays the internal key.
    DB_DIR / "072_batch_label.sql",
    # 073 sfg_box_pending_status — SFG boxes start PENDING; the per-box print
    # action flips them to PRINTED. Extends the sfg_box status CHECK to allow it.
    DB_DIR / "073_sfg_box_pending_status.sql",
    # 074 cr_line_sale_group_conversion — Customer-Returns line parity with the
    # legacy RTV line model: adds sale_group + conversion TEXT to cfpl/cdpl
    # customer_return_lines (dropped by the initial 070 port). Additive/idempotent.
    DB_DIR / "074_cr_line_sale_group_conversion.sql",
    # 075 rbac_notes_catalog — the "Roles & Permission" spec (Purchase /
    # Production / NPD) as auth_permission 4-tuples + role grants. Adds the
    # Material In, Planning-dispatch, Job batch-wise (overview/accounting/
    # quality/remark/box_printing/stage_chain/material_scan) and NPD edit/delete
    # rows the routers now gate on; creates the so_creator role. Middleware
    # unchanged. Idempotent (ON CONFLICT DO NOTHING).
    DB_DIR / "075_rbac_notes_catalog.sql",
    # 076 de-duplicates auth_permission. The UNIQUE (module, sub_module,
    # sub_sub_module, action) is NULLS DISTINCT (PG default), so ON CONFLICT
    # never suppresses re-inserts of NULL-column rows and this runner re-runs
    # every seed each deploy -> duplicate permissions -> duplicate checkboxes in
    # the admin permission tree. Registered LAST + idempotent: keeps the lowest
    # id per tuple, repoints grants, deletes the dupes. Self-heals every deploy.
    DB_DIR / "076_dedupe_permissions.sql",
    # 077 cr_email_routing — moves the Customer-Returns approval e-mail routing
    # (business-head / sales-POC / warehouse-owner / constant-CC / notify-to) out
    # of hardcoded literals into a DB table, served by GET
    # /api/v1/customer-returns/email-routing. Company-agnostic; idempotent seed.
    DB_DIR / "077_cr_email_routing.sql",
    # 078 cr_approval_audit — approval audit columns (approved_by/at/remark) on the
    # CR headers for the email magic-link + in-app approve flow, plus bare cold-unit
    # warehouse_cc rows for backend recipient matching. Additive/idempotent.
    DB_DIR / "078_cr_approval_audit.sql",
]

# ── Optional: drop the v1 legacy job-card stack (TEST / Supabase DB ONLY) ──────
# This runner is also the PROD migrator (run before each Lambda deploy), and 4 of
# the 5 v1 job-card tables (job_card, internal_job_card, job_card_byproduct,
# job_card_process_step) are STILL on live HTTP paths in app/modules/production —
# only job_card_material_accounting is dead. Dropping them unconditionally would
# break prod, so the drop is opt-in: it fires ONLY when DROP_LEGACY_V1 is set, e.g.
# when building the Supabase test DB that excludes the legacy stack:
#     DROP_LEGACY_V1=1 uv run python scripts/migrate.py
# Appended LAST so the tables exist (the migrations interleave them with current
# tables and can't skip them at CREATE time) before being dropped.
if os.environ.get("DROP_LEGACY_V1"):
    SQL_FILES.append(DB_DIR / "054_drop_legacy_v1_job_cards.sql")


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
