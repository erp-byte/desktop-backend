-- =========================================================================
-- Migration 072: operator-typed batch name on the accounting "Open Batch"
-- flow.
--
-- WHY:
--   batch_number (= job_card_phase_v2.phase_number) is a system-generated
--   integer (MAX+1) used numerically all over the stack — sorting, FG
--   dispatch, day-end. We keep it as the stable internal key and add a
--   separate free-text label the operator types when opening a batch. The
--   UI shows the label when present, falling back to "Batch <number>".
--
-- WHAT (idempotent):
--   1. ADD COLUMN batch_label TEXT on the underlying table (no-op if present).
--   2. DROP + CREATE job_card_batch_v2 to expose batch_label (mirrors 045's
--      full column list). Auto-updatable — single base table, no joins — so
--      open_batch's INSERT ... (batch_label) keeps working.
-- =========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.job_card_phase_v2') IS NULL THEN
        RAISE NOTICE 'job_card_phase_v2 absent — skipping batch_label add';
        RETURN;
    END IF;

    ALTER TABLE job_card_phase_v2
        ADD COLUMN IF NOT EXISTS batch_label TEXT;
END $$;

-- Recreate the view with the full 045 column set + batch_label. DROP+CREATE
-- (not CREATE OR REPLACE) so a live 045-era view without the new column is
-- replaced cleanly. Nothing depends on the view (045 dropped it the same way).
DROP VIEW IF EXISTS job_card_batch_v2;
CREATE VIEW job_card_batch_v2 AS
SELECT
    phase_id            AS batch_id,
    job_card_id,
    phase_number        AS batch_number,
    batch_label,
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
    'Phase→Batch compat view (036, extended by 045; 072 adds batch_label — '
    'the operator-typed free-text batch name). Auto-updatable.';

COMMIT;

-- ── Verification ──────────────────────────────────────────────────────
SELECT column_name FROM information_schema.columns
WHERE  table_name = 'job_card_batch_v2' AND column_name = 'batch_label';
-- expected: 1 row
