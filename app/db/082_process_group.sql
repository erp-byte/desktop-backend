-- 079_process_group.sql
-- Cross-product process merge (Plan List): when several products that share the
-- same factory + floor + raw-material articles are merged into ONE shared process
-- run, every job card created for that run is tagged with a shared
-- process_group_id. This lets the UI badge the merged rows and lets the merged
-- stage-1 RM indent aggregate across the group. Additive + idempotent.
--
-- The id itself is app-supplied (new_short_time_id(), same generator as job card
-- ids), so no sequence is needed here — just the column + a lookup index.
ALTER TABLE job_card_v2 ADD COLUMN IF NOT EXISTS process_group_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_jc_v2_process_group
    ON job_card_v2 (process_group_id)
    WHERE process_group_id IS NOT NULL;
