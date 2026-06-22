-- ===========================================================================
-- 056_sfg_master_enrichment.sql   (SFG / Job-Card integration — wire the 4
--   companion plug files that 050-055 left unused)
-- ---------------------------------------------------------------------------
-- 055 gave the catalogue VIEWS over data that was already loaded. This migration
-- adds the backing tables for the four plug files that were generated alongside
-- the loaded set (2026-06-17) but never ingested, then re-defines sfg_master to
-- surface the richer attributes:
--
--   sfg_attributes      <- JC_Stage1_Create_WIP_Master.csv  (the RICH catalogue:
--                          base_recipe, create_wip_operation, sfg_origin,
--                          consumed_by_fgs_count). NOTE: suggested_shelf_life_days
--                          is DELIBERATELY NOT ingested — per decision, WIP carries
--                          no shelf life (0 / no expiry); see inventory_service
--                          WIP_SHELF_LIFE_DAYS = 0.
--   stage_catalog       <- ProcessCategory_to_Operation.csv  (the §6 token ->
--                          operation/bucket/produces_sfg config seed the design
--                          ref recommends; today the classifier transcribes these
--                          9 rules into Python — this is the canonical reference).
--   fg_sfg_input_map    <- JC_Stage2_Final_FG_Packing.csv  (FG -> stage-2 input
--                          SFG####/RM, all 1085 FGs — reconciliation source for
--                          the sfg_where_used view, covers pack-only FGs too).
--   sfg_resolution_map  <- SFG_Resolution_Map.csv  (dedup provenance: which FG
--                          sample resolves to which SFG, num_fgs sharing count).
--
-- ID CONVENTION: these are STATIC REFERENCE/RECONCILIATION tables bulk-loaded by
-- master_ingest, NOT runtime-minted business entities — so they follow the
-- reference-table convention (natural text PK where unique, else a SERIAL
-- surrogate, exactly like bom_process_route.route_id), NOT the 8-digit
-- app-supplied BIGINT convention used for job cards / all_sku SFG rows / sfg_box.
--
-- The ROWS are loaded by master_ingest (ingest_sfg_attributes / _stage_catalog /
-- _fg_sfg_input_map / _sfg_resolution_map) at startup — this file is schema only.
-- Idempotent. Reversible via app/db/rollback/sfg_integration_down.sql.
-- (dep: 050 seam cols, 051 sfg_code, 055 sfg_master view)
-- ===========================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- (1) sfg_attributes — rich SFG catalogue attributes (JC_Stage1). sfg_code is
--     the natural key (1:1 with all_sku SFG rows). No shelf-life column.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sfg_attributes (
    sfg_code              TEXT PRIMARY KEY,            -- SFG#### (= all_sku.sfg_code)
    base_recipe           TEXT,                        -- pack-size-stripped recipe family
    create_wip_operation  TEXT,                        -- e.g. 'Blend & Form', 'Roasting'
    sfg_origin            TEXT,                        -- e.g. 'DERIVED_INLINE'
    item_group            TEXT,
    consumed_by_fgs_count INT,                         -- where-used fan-out (static snapshot)
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE sfg_attributes IS
    'Rich SFG catalogue attributes from JC_Stage1_Create_WIP_Master.csv; keyed to all_sku.sfg_code. Shelf life intentionally excluded.';

-- ---------------------------------------------------------------------------
-- (2) stage_catalog — the §6 canonical token -> operation/bucket map
--     (ProcessCategory_to_Operation.csv). 9 rules; token string is the key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stage_catalog (
    process_category_tokens TEXT PRIMARY KEY,          -- raw token(s), e.g. 'Blending + Bar Forming'
    practical_operation     TEXT,                      -- 'Blend & Form'
    stage_bucket            TEXT,                      -- 'Create WIP' | 'Final FG' | 'RM intake / Final FG'
    produces_sfg            BOOLEAN,
    note                    TEXT
);
COMMENT ON TABLE stage_catalog IS
    'Canonical Process-Category token -> practical_operation/stage_bucket/produces_sfg (design ref §6). Reference config; the classifier transcribes these rules.';

-- ---------------------------------------------------------------------------
-- (3) fg_sfg_input_map — FG -> its stage-2 opening input (JC_Stage2). Covers
--     all 1085 FGs incl. pack-only (input_code='RM'). Surrogate PK; fg_name is
--     NOT unique in the source (one collision), so (fg_name, input_code) is the
--     idempotency key.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fg_sfg_input_map (
    map_id            SERIAL PRIMARY KEY,
    fg_name           TEXT NOT NULL,
    cycle_archetype   TEXT,
    final_stage_label TEXT,
    input_code        TEXT,                            -- SFG#### or 'RM'
    input_sfg_article TEXT,
    input_kind        TEXT,                            -- SFG | RM
    UNIQUE (fg_name, input_code)
);
CREATE INDEX IF NOT EXISTS idx_fg_sfg_input_map_code ON fg_sfg_input_map(input_code)
    WHERE input_code IS NOT NULL;
COMMENT ON TABLE fg_sfg_input_map IS
    'FG -> stage-2 opening input (SFG#### or RM) from JC_Stage2_Final_FG_Packing.csv; reconciliation source for sfg_where_used.';

-- ---------------------------------------------------------------------------
-- (4) sfg_resolution_map — dedup provenance (SFG_Resolution_Map.csv): which FG
--     sample resolves to which SFG, and how many FGs share it (num_fgs).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sfg_resolution_map (
    resolution_id        SERIAL PRIMARY KEY,
    sfg_code             TEXT,                          -- SFG####
    fg_sample            TEXT,                          -- a representative consuming FG
    base_recipe          TEXT,
    create_wip_operation TEXT,
    sfg_origin           TEXT,
    sfg_article          TEXT,                          -- the SFG display article
    num_fgs              INT,                           -- how many FGs share this SFG
    UNIQUE (sfg_code, fg_sample)
);
CREATE INDEX IF NOT EXISTS idx_sfg_resolution_code ON sfg_resolution_map(sfg_code);
COMMENT ON TABLE sfg_resolution_map IS
    'Dedup provenance from SFG_Resolution_Map.csv: FG sample -> SFG#### resolution + num_fgs sharing count. Audit/governance.';

-- ---------------------------------------------------------------------------
-- (5) Re-define sfg_master to surface the JC_Stage1 attributes. Leading columns
--     are byte-identical to the 055 definition (CREATE OR REPLACE only ALLOWS
--     appending columns) so the dependent sfg_where_used view stays valid; the
--     3 new columns (base_recipe/create_wip_operation/sfg_origin) come from the
--     LEFT JOIN to sfg_attributes (NULL for any SFG without an attributes row).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW sfg_master AS
SELECT
    s.sku_id,
    s.sfg_code,
    s.particulars                          AS sfg_name,
    s.item_group,
    s.sub_group,
    'kg'::text                             AS uom,
    s.sale_group,
    s.gst,
    s.created_at,
    (SELECT MIN(COALESCE(r.practical_operation, r.process_name))
       FROM bom_process_route r
      WHERE r.output_code = s.sfg_code)     AS produced_at_stage,
    (SELECT COUNT(DISTINCT r2.bom_id)
       FROM bom_process_route r2
      WHERE r2.input_code = s.sfg_code)     AS consumed_by_fg_count,
    a.base_recipe,
    a.create_wip_operation,
    a.sfg_origin
FROM all_sku s
LEFT JOIN sfg_attributes a ON a.sfg_code = s.sfg_code
WHERE s.item_type = 'sfg'
  AND s.sfg_code IS NOT NULL;

COMMENT ON VIEW sfg_master IS
    'Dedicated SFG catalogue: all_sku(item_type=sfg) + sfg_attributes (base_recipe/operation/origin); design ref §8.1. Derived, not stored.';

COMMIT;
