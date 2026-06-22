-- ===========================================================================
-- 050_sfg_foundation.sql   (CANONICAL — SFG / Job-Card integration, Slice 1 & 3)
-- ---------------------------------------------------------------------------
-- The schema spine the SFG/WIP work rides on. Promoted from test_db into the
-- main app/db track + scripts/migrate.py SQL_FILES.
--
-- VERIFIED FACTS this migration relies on (do not "fix" them):
--   * There is NO CHECK constraint on item_type anywhere (bom_line / all_sku are
--     free-text TEXT). We only widen the COMMENT — there is no CHECK to alter.
--   * all_sku, bom_line, bom_process_route are created by the FOUNDATIONAL base
--     (schema.sql / production_schema.sql) — present in every build incl. the
--     migrate.py runner, so those ALTERs are safe unguarded.
--   * job_card_v2 is created by 017_job_card_v2.sql, which the migrate.py runner
--     ORPHANS. So the job_card_v2 ALTERs are GUARDED with to_regclass (the same
--     defensive pattern as 044/045/046): they no-op when v2 is absent and run
--     when it is present (prod has v2 out-of-band; the test_db build creates it).
--   * job_card_v2.input_kind/output_kind already allow 'SFG'/'WIP' (017) — no CHECK change.
--   * inventory_batch / floor_inventory already allow item_type='wip' — no DDL here.
--
-- Idempotent. Safe to re-run.
-- ===========================================================================
BEGIN;

-- (1) item_type domain is documentary only (free-text TEXT, no CHECK today).
COMMENT ON COLUMN all_sku.item_type  IS 'rm | pm | fg | sfg';
COMMENT ON COLUMN bom_line.item_type IS 'rm | pm | fg | sfg';

-- (2) Routing canonicalisation + chain-seam carriers on bom_process_route
--     (foundational table — present in every build). STEP 2 writes practical_operation
--     / stage_bucket; STEP 3 writes the kinds + SFG#### codes.
ALTER TABLE bom_process_route ADD COLUMN IF NOT EXISTS practical_operation TEXT;  -- 'Roast & Flavour/Salt', 'Blend & Form', ...
ALTER TABLE bom_process_route ADD COLUMN IF NOT EXISTS stage_bucket        TEXT;  -- 'Create WIP' | 'Final FG'
ALTER TABLE bom_process_route ADD COLUMN IF NOT EXISTS input_kind          TEXT;  -- RM | SFG | WIP
ALTER TABLE bom_process_route ADD COLUMN IF NOT EXISTS output_kind         TEXT;  -- SFG | WIP | FG
ALTER TABLE bom_process_route ADD COLUMN IF NOT EXISTS input_code          TEXT;  -- SFG#### opening input
ALTER TABLE bom_process_route ADD COLUMN IF NOT EXISTS output_code         TEXT;  -- SFG#### produced

-- (3) Option B (gate G1): SFG as a real bom_line input for costing/MRP.
--     Harmless if unused under Option A (flow-driven, recommended).
ALTER TABLE bom_line ADD COLUMN IF NOT EXISTS consumed_at_stage TEXT;             -- e.g. 'Final FG (opening RM)'

-- (4) SFG#### identity on the v2 chain seam: JC1.output_code -> JC2.input_code.
--     GUARDED — job_card_v2 is orphaned in the migrate.py runner (created by 017).
DO $$
BEGIN
    IF to_regclass('job_card_v2') IS NOT NULL THEN
        ALTER TABLE job_card_v2 ADD COLUMN IF NOT EXISTS input_code  TEXT;   -- SFG#### consumed by this stage
        ALTER TABLE job_card_v2 ADD COLUMN IF NOT EXISTS output_code TEXT;   -- SFG#### produced by this stage
    ELSE
        RAISE NOTICE '050_sfg_foundation: job_card_v2 absent (v2 chain not applied) - skipped input_code/output_code; re-run after 017_job_card_v2.sql';
    END IF;
END $$;

COMMIT;
