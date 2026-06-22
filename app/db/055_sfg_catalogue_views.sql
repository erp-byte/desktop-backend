-- ===========================================================================
-- 055_sfg_catalogue_views.sql   (SFG / Job-Card integration — catalogue layer)
-- ---------------------------------------------------------------------------
-- Closes the three "standalone catalogue artifact" gaps the SFG_DESIGN_REFERENCE
-- checklist lists (§8.1 SFG master, §9.2 FG↔stage↔SFG binding + SFG where-used)
-- WITHOUT duplicating data:
--
--   * The SFG catalogue already lives, deduped, in all_sku (item_type='sfg',
--     sfg_code) — loaded by ingest_sfg_master (051 + Python).
--   * The FG↔stage↔SFG binding already lives in bom_process_route.input_code /
--     output_code per step (stamped by ingest_jc_routing, 052 + Python),
--     joined to the FG via bom_header.
--
-- So these are VIEWS, not new physical tables. A second physical copy would need
-- a second ingest path and would drift from the runtime tables that the chain
-- engine actually reads/writes. Views give the checklist its named, queryable
-- artifacts (sfg_master / fg_sfg_binding / sfg_where_used) that are ALWAYS exactly
-- consistent with the live data, and tear down with a single DROP VIEW (see the
-- companion reversal script app/db/rollback/sfg_integration_down.sql).
--
-- Two partial indexes back the new where-used / binding access pattern. 052
-- deliberately deferred a reverse SFG#### index to "the slice that adds its
-- actual query" so the column order could match — this is that slice.
--
-- Idempotent. Safe to re-run. (dep: 050 seam columns, 051 sfg_code, 052 routing)
-- ===========================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- Indexes for the reverse SFG#### lookups the views/endpoints introduce.
-- Partial (only the few hundred rows that carry a code) — cheap to maintain.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_bom_route_input_code
    ON bom_process_route(input_code)  WHERE input_code  IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bom_route_output_code
    ON bom_process_route(output_code) WHERE output_code IS NOT NULL;

-- ---------------------------------------------------------------------------
-- RE-RUN SAFETY: 056 and 057 later APPEND columns to sfg_master. `CREATE OR
-- REPLACE VIEW` can ADD trailing columns but cannot SHRINK — so on a 2nd full
-- migrate run (the runner re-applies every file each deploy) this 055 definition
-- (fewer columns) would fail against the live 16-column view with
-- "ERROR: cannot drop columns from view". Drop the view (and its dependent
-- sfg_where_used) first so 055 always rebuilds the base shape; 056/057 then
-- re-append idempotently. fg_sfg_binding does not depend on sfg_master, so it
-- stays a plain CREATE OR REPLACE below.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS sfg_where_used;
DROP VIEW IF EXISTS sfg_master;

-- ---------------------------------------------------------------------------
-- (1) sfg_master — the dedicated SFG catalogue the checklist (§8.1) asks for,
--     projected from the single source of truth (all_sku, item_type='sfg').
--     UOM is surfaced as the literal 'kg' because all_sku.uom is NUMERIC(15,3)
--     and cannot hold the conceptual 'kg' unit (SFG rows store NULL there) —
--     every SFG in the dataset is kg (design ref §8.1).
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
    -- §8.1 process_stage: the Create-WIP operation that yields this SFG, taken
    -- from the authoritative routing seam (output_code = this SFG). An SFG#### is
    -- defined as (base recipe, stage), so every producing route agrees; MIN() is
    -- just a deterministic pick across shared producers.
    (SELECT MIN(COALESCE(r.practical_operation, r.process_name))
       FROM bom_process_route r
      WHERE r.output_code = s.sfg_code)     AS produced_at_stage,
    -- where-used fan-out at a glance: how many distinct FGs consume this SFG.
    (SELECT COUNT(DISTINCT r2.bom_id)
       FROM bom_process_route r2
      WHERE r2.input_code = s.sfg_code)     AS consumed_by_fg_count
FROM all_sku s
WHERE s.item_type = 'sfg'
  AND s.sfg_code IS NOT NULL;

COMMENT ON VIEW sfg_master IS
    'Dedicated SFG catalogue projected from all_sku(item_type=sfg); design ref §8.1. Derived, not stored.';

-- ---------------------------------------------------------------------------
-- (2) fg_sfg_binding — the FG↔stage↔SFG binding (design ref §9.2 / FG_Jobcard_
--     Chain). One row per routing step that produces or consumes an SFG####.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW fg_sfg_binding AS
SELECT
    r.bom_id,
    h.fg_sku_name,
    h.entity,
    h.customer_name,
    r.step_number,
    r.process_name,
    r.practical_operation,
    r.stage_bucket,
    r.input_kind,
    r.input_code,
    r.output_kind,
    r.output_code,
    COALESCE(r.output_code, r.input_code)  AS sfg_code,
    CASE WHEN r.output_code IS NOT NULL THEN 'produces'
         ELSE 'consumes'
    END                                    AS binding_role
FROM bom_process_route r
JOIN bom_header h ON h.bom_id = r.bom_id
WHERE r.output_code IS NOT NULL
   OR r.input_code  IS NOT NULL;

COMMENT ON VIEW fg_sfg_binding IS
    'FG↔stage↔SFG#### binding projected from bom_process_route seam; design ref §9.2. Derived, not stored.';

-- ---------------------------------------------------------------------------
-- (3) sfg_where_used — reverse index: SFG#### -> the FGs that consume it
--     (design ref §9.2 / SFG_Where_Used). Keyed on the consuming step's
--     input_code, the authoritative chain signal the runtime uses.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW sfg_where_used AS
SELECT
    r.input_code                                    AS sfg_code,
    sm.sfg_name,
    r.bom_id,
    h.fg_sku_name,
    h.entity,
    r.step_number                                   AS consumed_at_step,
    COALESCE(r.practical_operation, r.process_name) AS consumed_at_stage
FROM bom_process_route r
JOIN bom_header h       ON h.bom_id = r.bom_id
LEFT JOIN sfg_master sm ON sm.sfg_code = r.input_code
WHERE r.input_code IS NOT NULL;

COMMENT ON VIEW sfg_where_used IS
    'Reverse index SFG#### -> consuming FGs, from bom_process_route.input_code; design ref §9.2. Derived, not stored.';

COMMIT;
