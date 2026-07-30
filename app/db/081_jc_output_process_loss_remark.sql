-- Migration 081: add process_loss_remark to job_card_output_v2
--
-- record_output (job_card_v2.py) writes a free-text process_loss_remark
-- alongside the FG output row (the operator's note explaining a process loss),
-- and the JC detail GET reads it back via section_5_output.process_loss_remark.
-- The code shipped without this column, so record_output 500s on any DB where
-- it is missing (UndefinedColumnError). Nullable TEXT — sibling to the
-- process_loss_kg column added in migration 026. Idempotent.
ALTER TABLE job_card_output_v2
    ADD COLUMN IF NOT EXISTS process_loss_remark TEXT;

-- Verify (expect one row):
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'job_card_output_v2' AND column_name = 'process_loss_remark';
