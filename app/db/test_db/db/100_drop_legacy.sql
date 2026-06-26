-- ===========================================================================
-- 100_drop_legacy.sql   (TEST DB — applied LAST)
-- ---------------------------------------------------------------------------
-- "don't make the legacy tables." The build creates the full schema (the legacy
-- v1 job-card tables are interwoven with current tables in production_schema.sql,
-- so they can't be skipped at CREATE time); we drop them at the very end instead.
--
-- The 5 tables below are tagged "JC · v1 legacy" in Candor_System_Wiring.html and
-- are the superseded v1 job-card stack. CASCADE drops only the FK CONSTRAINTS on
-- other tables that point at these (e.g. sample_requisitions.linked_job_card_id) —
-- NOT the other tables themselves. The live v2 chain (job_card_v2 + satellites) is
-- unaffected. Idempotent (IF EXISTS).
-- ===========================================================================

DROP TABLE IF EXISTS job_card_process_step        CASCADE;
DROP TABLE IF EXISTS job_card_material_accounting  CASCADE;
DROP TABLE IF EXISTS job_card_byproduct           CASCADE;
DROP TABLE IF EXISTS internal_job_card            CASCADE;
DROP TABLE IF EXISTS job_card                     CASCADE;

-- NOTE: the wiring ALSO tags `job_card_consumption_variance_v2` as "JC · v1 legacy",
-- but it is a LIVE v2 table created by 028_framework and used by current code
-- (consumption-variance staging — see project memory). It is intentionally KEPT.
-- To honour the wiring tag literally, uncomment the next line:
-- DROP TABLE IF EXISTS job_card_consumption_variance_v2 CASCADE;
