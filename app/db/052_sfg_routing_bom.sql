-- ===========================================================================
-- 052_sfg_routing_bom.sql   (SFG / Job-Card integration, Slice 3)
-- ---------------------------------------------------------------------------
-- The 2-stage routing chain + the SFG seam. The CARRIER columns
-- (practical_operation, stage_bucket, input_kind, output_kind, input_code,
-- output_code) were already added to bom_process_route by 050_sfg_foundation.
-- This migration is therefore SCHEMA-SUPPORT ONLY:
--
--   * indexes that make the seam lookups cheap — resolving "the SFG#### a
--     2-stage article produces" at job-card creation, and the reverse "which
--     FGs consume SFG####" used by the chain view / reporting;
--   * documentary COMMENTs pinning the Slice-3 semantics.
--
-- The 1460 ROUTING ROWS (1085 articles; 375 two-stage) are loaded by
-- master_ingest.ingest_jc_routing() at app startup — NOT here. A static SQL
-- migration can't normalise the article name (NBSP/mojibake) before the
-- exact-string join to bom_header, nor resolve article -> bom_id. The loader
-- replaces each plug article's route with its authoritative 1-or-2 step chain
-- (Create WIP -> Final FG) and stamps the seam columns. Same split-of-labour
-- as 051 (catalogue schema here / SFG rows minted in Python).
--
-- NO new INDEX here (deliberate): the only seam lookup is
-- _resolve_sfg_seam_code (job_card_v2), which filters bom_id + output_kind +
-- output_code-IS-NOT-NULL and is already served by the pre-existing
-- idx_bom_route_bom(bom_id) and UNIQUE(bom_id, step_number) — there are only
-- 1-or-2 rows per bom, so a code-keyed index would never be chosen by the
-- planner and would only add write overhead to the ingest upsert. A
-- reverse "which FGs consume SFG####" index belongs in the slice that adds
-- its actual query (Slice 7 reporting), so its column order can match that
-- access pattern.
--
-- This migration is therefore documentary: it pins the Slice-3 column
-- semantics on bom_process_route. Idempotent. (dep: 050 columns, 051 catalogue)
-- ===========================================================================
BEGIN;

-- Column intent (documentary only — these are free-text TEXT, no CHECK):
COMMENT ON COLUMN bom_process_route.input_kind   IS 'RM | SFG | WIP — opening-material kind for this step';
COMMENT ON COLUMN bom_process_route.output_kind  IS 'SFG | WIP | FG — produced-material kind for this step';
COMMENT ON COLUMN bom_process_route.input_code   IS 'SFG#### consumed at the start of this step (NULL when input_kind=RM)';
COMMENT ON COLUMN bom_process_route.output_code  IS 'SFG#### produced by this step (set on Create-WIP steps; NULL on Final FG)';

COMMIT;
