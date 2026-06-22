-- ===========================================================================
-- 054_drop_legacy_v1_job_cards.sql   (TEST / Supabase DB only — applied LAST)
-- ---------------------------------------------------------------------------
-- Drops the 5 v1 "legacy" job-card tables (tagged "JC · v1 legacy" in
-- Candor_System_Wiring.html). This is the main-track promotion of
-- test_db/db/100_drop_legacy.sql so the drop runs as part of scripts/migrate.py.
--
-- !! NOT SAFE ON THE PROD DB !!
-- A reference audit (2026-06-18) found 4 of these 5 tables are STILL on live
-- HTTP read/write paths in app/modules/production (job-card generate/list,
-- dashboards, output recording, complete-step, day-end, discrepancy, AI planner,
-- internal-order creation, QR material receipt). Dropping them on the DB the
-- FastAPI app talks to would 500 those endpoints. The build creates them because
-- the migrations interleave the v1 tables with current ones and can't skip them
-- at CREATE time — so we drop them at the very end, and ONLY when explicitly
-- building the Supabase test DB.
--
-- migrate.py gates this file behind the DROP_LEGACY_V1 env var so it never fires
-- on a prod (RDS) deploy:
--     DROP_LEGACY_V1=1 uv run python scripts/migrate.py
--
-- CASCADE drops only the FK CONSTRAINTS on other tables that point at these
-- (e.g. sample_requisitions.linked_job_card_id) — NOT the other tables. The live
-- v2 chain (job_card_v2 + satellites) is unaffected. Idempotent (IF EXISTS).
-- ===========================================================================

DROP TABLE IF EXISTS job_card_process_step         CASCADE;
DROP TABLE IF EXISTS job_card_material_accounting   CASCADE;
DROP TABLE IF EXISTS job_card_byproduct            CASCADE;
DROP TABLE IF EXISTS internal_job_card             CASCADE;
DROP TABLE IF EXISTS job_card                      CASCADE;

-- NOTE: the wiring ALSO tags `job_card_consumption_variance_v2` as "JC · v1 legacy",
-- but it is a LIVE v2 table (created by 028_framework, used by current consumption-
-- variance staging code). It is intentionally KEPT — do NOT add it here.
