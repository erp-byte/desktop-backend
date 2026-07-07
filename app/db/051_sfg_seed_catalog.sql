-- ===========================================================================
-- 051_sfg_seed_catalog.sql   (CANONICAL — SFG / Job-Card integration, Slice 1)
-- ---------------------------------------------------------------------------
-- The SFG catalogue SCHEMA spine on all_sku. The 343 SFG ROWS themselves are
-- loaded by master_ingest.ingest_sfg_master() at app startup (NOT here), so that
-- every new SFG row gets an application-supplied 8-digit BIGINT primary key via
-- new_short_time_id() + insert_with_pk_retry() — the same id convention used
-- across the codebase (so_line, ncr, dev job cards, …). A static SQL migration
-- cannot mint time-based ids with collision-retry, so the data load lives in
-- Python; this file only prepares the columns it needs.
--
-- VERIFIED FACTS this migration relies on (do not "fix" them):
--   * all_sku.sku_id is SERIAL (int4) in the base schema (schema.sql). We WIDEN
--     it to BIGINT so app-supplied 8-digit ids are first-class. The SERIAL
--     default (nextval) is intentionally KEPT — every OTHER all_sku insert site
--     (catalogue loader, seed_test_data, sample_e2e) is untouched and keeps
--     auto-assigning ids. Only NEW SFG rows supply an explicit 8-digit BIGINT.
--   * Widening the PK to BIGINT does NOT require dropping the 6 INT foreign keys
--     that reference all_sku(sku_id) (samples 037/041/047): Postgres keeps INT
--     FKs valid against a BIGINT PK, and 8-digit ids fit int4 anyway. So existing
--     rows + FKs are untouched (no re-id).
--   * all_sku has NO sfg_code column in the base schema and NO UNIQUE on
--     particulars. The SFG#### code is the stable join key used by the v2 chain
--     seam (job_card_v2.input_code/output_code, bom_process_route) and by
--     sfg_box.sfg_code (053). We add it here.
--   * There is NO CHECK on item_type anywhere; 'sfg' is free-text (see 050).
--
-- Forward-only (matches every migration in this repo — there are no down-
-- migrations). The data-side reclassification is done by ingest_sfg_master,
-- which LOGS each re-typed row's prior item_type for a manual revert if ever
-- needed; net-new SFG rows are distinguishable by their 8-digit sku_id.
--
-- Idempotent. Safe to re-run.
-- ===========================================================================
BEGIN;

-- (1) Widen the catalogue PK to BIGINT so 8-digit app-supplied SFG ids are
--     first-class. Guarded so a re-run never attempts a needless table rewrite.
DO $$
BEGIN
    IF (SELECT data_type
          FROM information_schema.columns
         WHERE table_name = 'all_sku' AND column_name = 'sku_id') = 'integer' THEN
        ALTER TABLE all_sku ALTER COLUMN sku_id TYPE BIGINT;
    END IF;
END $$;

-- (2) Business key for the SFG catalogue.
ALTER TABLE all_sku ADD COLUMN IF NOT EXISTS sfg_code TEXT;
COMMENT ON COLUMN all_sku.sfg_code IS 'SFG#### business key for item_type=sfg rows; NULL for rm/pm/fg';

-- One code -> one catalogue row (partial: only sfg rows carry a code).
CREATE UNIQUE INDEX IF NOT EXISTS uq_all_sku_sfg_code
    ON all_sku(sfg_code) WHERE sfg_code IS NOT NULL;

COMMIT;
