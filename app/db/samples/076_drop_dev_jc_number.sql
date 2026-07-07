-- 076_drop_dev_jc_number.sql
-- Retire the NPDJC-YYYYMMDD-NNNN house number on dev job cards.
--
-- The 8-digit time-based BIGINT id (new_short_time_id, supplied at insert) is now
-- the single canonical identifier everywhere — gate pass / outpass, mail, WhatsApp,
-- BOM promotion notes, FG-sample lot numbers and dispatch notes all reference id.
-- The dev_jc_number column + its sequence are no longer written or read, so drop
-- both. Already-minted FG-sample batches keep the NPDJC string they copied into
-- their own lot_number column (a separate column, untouched here).

ALTER TABLE npd_dev_job_cards DROP COLUMN IF EXISTS dev_jc_number;
DROP SEQUENCE IF EXISTS seq_npd_dev_jc;
