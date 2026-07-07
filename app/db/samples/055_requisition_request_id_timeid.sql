-- 055_requisition_request_id_timeid.sql
-- request_id becomes an APP-SUPPLIED, TIME-based 8-digit BIGINT (the same
-- new_short_time_id() pattern as job_card_id / plan_id — see app/core/helpers.py),
-- replacing the generated 10000000+id column from migration 052.
--   • drop the generated column (only if it's still generated);
--   • re-add it as a plain BIGINT;
--   • backfill existing rows with their prior value (10000000+id) so already-
--     shown ids don't shift; new rows get a time-based id from the service
--     (with a uniqueness retry).
-- Idempotent: the DROP only fires while the column is generated, the backfill
-- only touches NULLs (so service-supplied time ids are never overwritten).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'sample_requisitions' AND column_name = 'request_id'
                 AND is_generated = 'ALWAYS') THEN
        ALTER TABLE sample_requisitions DROP COLUMN request_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'sample_requisitions' AND column_name = 'request_id') THEN
        ALTER TABLE sample_requisitions ADD COLUMN request_id BIGINT;
    END IF;
END $$;

UPDATE sample_requisitions SET request_id = 10000000 + id WHERE request_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sample_req_request_id
    ON sample_requisitions(request_id);
