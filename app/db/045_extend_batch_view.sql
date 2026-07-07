-- =========================================================================
-- Migration 045: ensure job_card_batch_v2 view exposes the Stage-2 batch
-- summary columns (process_loss_kg, fg_actual_kg, fg_actual_units, etc.)
--
-- WHY:
--   Migration 036_jc_batch_compat_shim.sql created job_card_batch_v2 as a
--   view over job_card_phase_v2 with only the legacy column set. Migration
--   038_jc_batch_per_record.sql recreates the view to include the Stage-2
--   per-batch summary columns (input_qty_kg, fg_actual_kg, fg_actual_units,
--   process_loss_kg, control_sample_kg, is_balanced, balance_difference_qty,
--   closure_remarks, opened_at_ist, closed_at_ist, ended_at_ist) — but
--   038_jc_batch_per_record.sql was never wired into scripts/migrate.py.
--
--   close_batch UPDATEs process_loss_kg directly on job_card_phase_v2 (the
--   underlying table), which works because someone hand-applied the column
--   additions on prod. But GET /job-cards-v2/{id}/batches reads from the
--   VIEW, and if the view definition stops at 036's column set, the
--   response omits process_loss_kg / fg_actual_kg / fg_actual_units —
--   so the form re-hydrates with empty fields on closed batches even
--   though the DB row carries the values.
--
-- WHAT (idempotent, safe to re-run):
--   1. Confirm the underlying table has the new columns; add them as
--      no-ops if missing (mirrors the additions 038_jc_batch_per_record
--      would have made).
--   2. CREATE OR REPLACE the view with the full Stage-2 column list so
--      list_batches returns them.
--
--   Guarded with to_regclass + column existence checks so a fresh DB
--   without job_card_phase_v2 no-ops.
-- =========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.job_card_phase_v2') IS NULL THEN
        RAISE NOTICE 'job_card_phase_v2 absent — skipping view extension';
        RETURN;
    END IF;

    -- 1. Backfill any Stage-2 columns that 038_jc_batch_per_record would
    --    have added but never did because it isn't in migrate.py.
    ALTER TABLE job_card_phase_v2
        ADD COLUMN IF NOT EXISTS input_qty_kg            NUMERIC(15,3),
        ADD COLUMN IF NOT EXISTS fg_actual_kg            NUMERIC(15,3),
        ADD COLUMN IF NOT EXISTS fg_actual_units         NUMERIC(15,3),
        ADD COLUMN IF NOT EXISTS process_loss_kg         NUMERIC(15,3),
        ADD COLUMN IF NOT EXISTS control_sample_kg       NUMERIC(15,3),
        ADD COLUMN IF NOT EXISTS is_balanced             BOOLEAN,
        ADD COLUMN IF NOT EXISTS balance_difference_qty  NUMERIC(15,3),
        ADD COLUMN IF NOT EXISTS closure_remarks         TEXT,
        ADD COLUMN IF NOT EXISTS opened_at_ist           TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS closed_at_ist           TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS ended_at_ist            TIMESTAMPTZ;
END $$;

-- 2. Recreate the view to expose every Stage-2 column. CREATE OR REPLACE
-- requires the column list to be a strict superset of the existing one,
-- so we DROP + CREATE to handle the case where 036's narrower view is
-- still live. Auto-updatable because it's a single base table, no
-- DISTINCT / aggregates / joins.
DROP VIEW IF EXISTS job_card_batch_v2;
CREATE VIEW job_card_batch_v2 AS
SELECT
    phase_id            AS batch_id,
    job_card_id,
    phase_number        AS batch_number,
    phase_date          AS batch_date,
    planned_qty_kg,
    produced_qty_kg,
    extra_give_away_qty,
    started_at,
    ended_at,
    status,
    closed_by,
    closed_at,
    notes,
    -- Stage-2 per-batch summary columns. Migration 038's intent.
    input_qty_kg,
    fg_actual_kg,
    fg_actual_units,
    process_loss_kg,
    control_sample_kg,
    is_balanced,
    balance_difference_qty,
    closure_remarks,
    opened_at_ist,
    closed_at_ist,
    ended_at_ist
FROM job_card_phase_v2;

COMMENT ON VIEW job_card_batch_v2 IS
    'Phase→Batch compat view (migration 036, extended by 045 to expose '
    'Stage-2 batch-summary columns that 038_jc_batch_per_record never '
    'wired in). Auto-updatable.';

COMMIT;

-- ── Verification ──────────────────────────────────────────────────────
-- All Stage-2 columns must appear on the view.
SELECT column_name
FROM   information_schema.columns
WHERE  table_name = 'job_card_batch_v2'
  AND  column_name IN (
           'process_loss_kg', 'fg_actual_kg', 'fg_actual_units',
           'control_sample_kg', 'is_balanced', 'balance_difference_qty',
           'closure_remarks', 'input_qty_kg'
       )
ORDER  BY column_name;
-- expected: 8 rows
