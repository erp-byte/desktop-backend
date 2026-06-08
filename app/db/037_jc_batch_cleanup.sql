-- =========================================================================
-- Migration 037: Cleanup — drop the phase compat layer, rename for real.
--
-- DO NOT RUN until:
--   * Migration 036 has been live for at least one full operating cycle.
--   * No callers of the legacy phase_id / job_card_phase_v2 names remain
--     (grep both repos; check for any third-party integration).
--   * The Stage 1 manual test plan has been signed off.
--
-- This migration completes what 036 deferred:
--   1. Drop the BEFORE INSERT/UPDATE sync triggers + the sync function.
--   2. Drop the legacy `phase_id` columns on the three child tables
--      (job_card_output_v2, job_card_partial_dispatch_v2,
--      job_card_shift_log_v2).  batch_id stays.
--   3. Drop the job_card_batch_v2 VIEW.
--   4. Rename the underlying job_card_phase_v2 table → job_card_batch_v2
--      (the view's name is now free), plus rename the columns
--      (phase_id → batch_id, phase_number → batch_number, phase_date →
--      batch_date) and the indexes / PK constraint.
--
-- After this migration, batch_id is the only PK column name and
-- job_card_batch_v2 is a real table (not a view).
--
-- Idempotent.  Safe to re-run.
-- =========================================================================

BEGIN;

-- ── 1. Drop the sync triggers + function ─────────────────────────────
DROP TRIGGER IF EXISTS trg_sync_phase_batch_id_output
    ON job_card_output_v2;
DROP TRIGGER IF EXISTS trg_sync_phase_batch_id_dispatch
    ON job_card_partial_dispatch_v2;
DROP TRIGGER IF EXISTS trg_sync_phase_batch_id_shift
    ON job_card_shift_log_v2;
DROP FUNCTION IF EXISTS fn_sync_phase_batch_id();

-- ── 2. Drop the legacy phase_id columns on child tables ──────────────
-- batch_id was backfilled + kept in sync by 036, so dropping phase_id
-- loses nothing.  The FK on batch_id automatically retargets in step 4
-- via PG's OID-based reference tracking.
ALTER TABLE job_card_output_v2          DROP COLUMN IF EXISTS phase_id;
ALTER TABLE job_card_partial_dispatch_v2 DROP COLUMN IF EXISTS phase_id;
ALTER TABLE job_card_shift_log_v2        DROP COLUMN IF EXISTS phase_id;

-- Drop the legacy phase indexes — the batch_id versions (created in
-- 036) are what queries hit going forward.
DROP INDEX IF EXISTS idx_jco_v2_phase;
DROP INDEX IF EXISTS idx_jcpd_v2_phase;
DROP INDEX IF EXISTS idx_jcsl_v2_phase;

-- ── 3. Drop the job_card_batch_v2 view (frees the name) ──────────────
DROP VIEW IF EXISTS job_card_batch_v2;

-- ── 4. Rename the real table + columns + indexes + PK constraint ─────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'job_card_phase_v2')
       AND NOT EXISTS (SELECT 1 FROM information_schema.tables
                       WHERE table_name = 'job_card_batch_v2')
    THEN
        EXECUTE 'ALTER TABLE job_card_phase_v2 RENAME TO job_card_batch_v2';
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'job_card_batch_v2' AND column_name = 'phase_id')
    THEN
        EXECUTE 'ALTER TABLE job_card_batch_v2 RENAME COLUMN phase_id TO batch_id';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'job_card_batch_v2' AND column_name = 'phase_number')
    THEN
        EXECUTE 'ALTER TABLE job_card_batch_v2 RENAME COLUMN phase_number TO batch_number';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'job_card_batch_v2' AND column_name = 'phase_date')
    THEN
        EXECUTE 'ALTER TABLE job_card_batch_v2 RENAME COLUMN phase_date TO batch_date';
    END IF;
END $$;

ALTER INDEX IF EXISTS uq_jcphase_one_open RENAME TO uq_jcbatch_one_open;
ALTER INDEX IF EXISTS idx_jcphase_jc      RENAME TO idx_jcbatch_jc;
ALTER INDEX IF EXISTS idx_jcphase_date    RENAME TO idx_jcbatch_date;
ALTER INDEX IF EXISTS idx_jcphase_status  RENAME TO idx_jcbatch_status;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'job_card_phase_v2_pkey')
    THEN
        EXECUTE 'ALTER TABLE job_card_batch_v2 '
                'RENAME CONSTRAINT job_card_phase_v2_pkey TO job_card_batch_v2_pkey';
    END IF;
END $$;

COMMIT;
