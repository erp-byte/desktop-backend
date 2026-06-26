-- =========================================================================
-- Migration 019: Align all v2 table PKs to app-supplied 8-digit BIGINTs.
--
-- Until now the v2 sub-tables (plan_line, plan_step, shift_log, output,
-- dispatch, indents, sign-offs, accounting) used BIGSERIAL defaults. The
-- top-level v2 entities (so_fulfillment_v2, production_plan_v2,
-- job_card_v2) have been switched to app-supplied time-based BIGINTs;
-- this migration aligns the sub-tables to the same pattern so every
-- ID in the v2 surface is consistent.
--
-- Mechanism: BIGSERIAL is just BIGINT + a default of nextval(sequence).
-- DROP DEFAULT keeps the column as BIGINT but stops auto-incrementing;
-- the application is now responsible for supplying the value via
-- new_short_time_id() + insert_with_pk_retry. Existing rows keep their
-- small sequence-generated IDs (1..N); new inserts get 8-digit time IDs.
-- Sequences are LEFT IN PLACE — orphan, but harmless and trivially
-- reversible if we ever need to fall back.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- ── 009 — planning v2 sub-tables ───────────────────────────────────────
ALTER TABLE so_revision_log_v2
    ALTER COLUMN revision_id   DROP DEFAULT;
ALTER TABLE production_plan_line_v2
    ALTER COLUMN plan_line_id  DROP DEFAULT;

-- ── 011 — planning v2 carryforward + override tables ──────────────────
ALTER TABLE fulfillment_bom_override_v2
    ALTER COLUMN override_id   DROP DEFAULT;
ALTER TABLE fulfillment_floor_stock_v2
    ALTER COLUMN floor_stock_id DROP DEFAULT;

-- ── 012 — planning v2 step rows ───────────────────────────────────────
ALTER TABLE production_plan_step_v2
    ALTER COLUMN step_id       DROP DEFAULT;

-- ── 017 — job card v2 sub-tables ──────────────────────────────────────
ALTER TABLE job_card_shift_log_v2
    ALTER COLUMN log_id        DROP DEFAULT;
ALTER TABLE job_card_output_v2
    ALTER COLUMN output_id     DROP DEFAULT;
ALTER TABLE job_card_partial_dispatch_v2
    ALTER COLUMN dispatch_id   DROP DEFAULT;
ALTER TABLE job_card_rm_indent_v2
    ALTER COLUMN rm_indent_id  DROP DEFAULT;
ALTER TABLE job_card_pm_indent_v2
    ALTER COLUMN pm_indent_id  DROP DEFAULT;
ALTER TABLE job_card_sign_off_v2
    ALTER COLUMN sign_off_id   DROP DEFAULT;

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
-- All listed PK columns should report column_default = NULL.
SELECT table_name, column_name, column_default, udt_name
FROM   information_schema.columns
WHERE  column_name IN (
           'revision_id', 'plan_line_id', 'override_id', 'floor_stock_id',
           'step_id', 'log_id', 'output_id', 'dispatch_id',
           'rm_indent_id', 'pm_indent_id', 'sign_off_id'
       )
  AND  table_name LIKE '%_v2'
ORDER  BY table_name, column_name;
