-- 060_dev_jc_bigint_id.sql
-- The standalone NPD development job card's id becomes an app-supplied 8-digit
-- time-based BIGINT (new_short_time_id — the same handle pattern as
-- sample_requisitions.request_id / job_card_id / plan_id), instead of a SERIAL.
-- We widen the id + the lines FK column to BIGINT and drop the SERIAL default so
-- the service supplies the id explicitly (with a unique retry). Existing small
-- ids stay valid; new cards get 8-digit ids (no collision). Idempotent.

DO $$
DECLARE
    fk_name text;
BEGIN
    -- Drop the lines -> cards FK (whatever its generated name) so both sides can
    -- be widened to BIGINT together.
    SELECT conname INTO fk_name
      FROM pg_constraint
     WHERE conrelid = 'npd_dev_job_card_lines'::regclass
       AND contype  = 'f'
       AND confrelid = 'npd_dev_job_cards'::regclass;
    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE npd_dev_job_card_lines DROP CONSTRAINT %I', fk_name);
    END IF;
END $$;

ALTER TABLE npd_dev_job_card_lines ALTER COLUMN dev_jc_id TYPE BIGINT;

ALTER TABLE npd_dev_job_cards ALTER COLUMN id DROP DEFAULT;   -- stop using the SERIAL sequence
ALTER TABLE npd_dev_job_cards ALTER COLUMN id TYPE BIGINT;

-- Re-add the FK (fixed name so a second run drops it cleanly above and re-adds here).
ALTER TABLE npd_dev_job_card_lines
    ADD CONSTRAINT npd_dev_job_card_lines_dev_jc_id_fkey
    FOREIGN KEY (dev_jc_id) REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE;
