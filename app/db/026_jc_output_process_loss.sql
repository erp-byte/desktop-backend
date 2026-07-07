-- =========================================================================
-- Migration 026: add process_loss_kg to job_card_output_v2
--
-- The legacy v1 client (Android Consumption app, OutputAccountingFragment)
-- sends `process_loss_kg` alongside the FG output row and reads it back
-- via `section_5_output.process_loss_kg` on the JC detail GET. The v2
-- output table created in migration 017 had no column for it, so saves
-- silently dropped the value and the field always re-loaded blank.
--
-- Add the column with a sensible default so existing rows backfill to 0.
-- The v2 endpoint will persist the value here on save and the detail GET
-- will surface it in section_5_output.process_loss_kg.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

ALTER TABLE job_card_output_v2
    ADD COLUMN IF NOT EXISTS process_loss_kg NUMERIC(15,3) NOT NULL DEFAULT 0;

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
SELECT column_name, data_type, column_default
FROM   information_schema.columns
WHERE  table_name = 'job_card_output_v2'
  AND  column_name = 'process_loss_kg';
