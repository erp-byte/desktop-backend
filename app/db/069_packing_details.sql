-- 069_packing_details.sql
-- Packing Details module: one row per packing record. batch_code + article_name
-- are the business handles; `details` is a free-form JSONB body the app fills.
--
-- packing_id is an app-supplied 8-digit time-based BIGINT minted by
-- new_short_time_id() (last 8 digits of epoch-ms) and inserted through
-- insert_with_pk_retry() — the SAME identifier scheme the job-card v2 stack
-- uses (see 019_v2_app_supplied_ids.sql). A plain BIGINT PK with no sequence
-- default: the app supplies the value, so no BIGSERIAL / nextval here.
--
-- Additive + idempotent (safe to re-run under scripts/migrate.py).
CREATE TABLE IF NOT EXISTS packing_details (
    packing_id   BIGINT PRIMARY KEY,                 -- app-supplied 8-digit time id
    batch_code   TEXT  NOT NULL,
    article_name TEXT  NOT NULL,
    details      JSONB NOT NULL DEFAULT '{}'::jsonb, -- free-form JSON body
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Lookup helpers for the two business handles the list endpoint filters on.
CREATE INDEX IF NOT EXISTS idx_packing_details_batch
    ON packing_details (batch_code);
CREATE INDEX IF NOT EXISTS idx_packing_details_article
    ON packing_details (article_name);
