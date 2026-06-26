-- =========================================================================
-- Migration 031: B5 force-close audit columns + B10 list-sort indexes
--
-- B5 C2 / C1 audit trail:
--   job_card_v2 gains four columns recording an R8 unbalanced-close
--   override: who, when, and which bom_amendment_request_v2 row
--   authorised it. force_closed BOOLEAN is the queryable flag so dashboards
--   can split clean-close from forced-close in a single predicate. The
--   force_close_request_id FK ties the JC back to the maker-checker row
--   (B11 will validate the row's status at close time).
--
-- B10 H3 indexes:
--   The R3.D list extensions added sort_by + date_field. Without indexes
--   on created_at / start_time / end_time the default ORDER BY does a
--   sort over the whole filtered set on every page request. Partial
--   indexes WHERE deleted_at IS NULL match the standard query path.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- B5 audit columns
ALTER TABLE job_card_v2
    ADD COLUMN IF NOT EXISTS force_closed           BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS force_close_request_id BIGINT
        REFERENCES bom_amendment_request_v2(request_id),
    ADD COLUMN IF NOT EXISTS force_close_by         TEXT,
    ADD COLUMN IF NOT EXISTS force_close_at         TIMESTAMPTZ;

COMMENT ON COLUMN job_card_v2.force_closed IS
    'TRUE when /complete was honored with an R8 unbalanced-close override. '
    'force_close_request_id, force_close_by, force_close_at carry the audit.';

CREATE INDEX IF NOT EXISTS idx_jc_v2_force_closed
    ON job_card_v2(force_close_at DESC)
    WHERE force_closed = TRUE;

-- B10 sort/date indexes (partial - deleted JCs are excluded by every list
-- query already, so the partial predicate keeps the index small).
CREATE INDEX IF NOT EXISTS idx_jc_v2_created_at
    ON job_card_v2(created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_jc_v2_start_time
    ON job_card_v2(start_time DESC)
    WHERE deleted_at IS NULL AND start_time IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jc_v2_end_time
    ON job_card_v2(end_time DESC)
    WHERE deleted_at IS NULL AND end_time IS NOT NULL;

COMMIT;
