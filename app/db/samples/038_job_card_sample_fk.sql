-- =========================================================================
-- Migration 038: Sample linkage on the legacy job_card table
--
-- Extends job_card (the live lifecycle table written by
-- job_card_engine.create_job_cards()) so sample FG / NPD job cards can be
-- traced back to their requisition and filtered out of regular production
-- reports. Also adds the deferred sample_requisitions.linked_job_card_id FK
-- (the column was created as a plain INT in 037).
--
-- Split from 037 only because it touches a pre-existing, heavily-used table;
-- kept separate so the sample core schema (037) can land independently.
--
-- Depends on: production_schema.sql (job_card), 037 (sample_requisitions).
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- 1. job_card extensions --------------------------------------------------
ALTER TABLE job_card
    ADD COLUMN IF NOT EXISTS sample_requisition_id INT REFERENCES sample_requisitions(id),
    ADD COLUMN IF NOT EXISTS jobcard_type TEXT NOT NULL DEFAULT 'REGULAR'
        CHECK (jobcard_type IN ('REGULAR','BASIS_FG_SAMPLE','NPD_SAMPLE','INTERNAL_FG_OVERRIDE'));

CREATE INDEX IF NOT EXISTS idx_job_card_sample_req ON job_card(sample_requisition_id)
    WHERE sample_requisition_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_job_card_type ON job_card(jobcard_type)
    WHERE jobcard_type <> 'REGULAR';

-- 2. Deferred FK: sample_requisitions.linked_job_card_id -> job_card -------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sample_req_job_card') THEN
        ALTER TABLE sample_requisitions
            ADD CONSTRAINT fk_sample_req_job_card
            FOREIGN KEY (linked_job_card_id) REFERENCES job_card(job_card_id);
    END IF;
END $$;

COMMIT;
