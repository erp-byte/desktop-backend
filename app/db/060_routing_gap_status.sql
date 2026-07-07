-- ===========================================================================
-- 060_routing_gap_status.sql   (SFG / Job-Card integration — Routing-Gap layer)
-- ---------------------------------------------------------------------------
-- The LIVE counterpart to the offline reconciliation gap list. Slice 7 promoted
-- ~108 of the ~450 gap articles into "FG Master" (bom_header rows carrying a
-- `process_category` + derived bom_process_route steps, stamped
-- promoted_from_gap — see 058 + scripts/promote_fg_master_gaps.py). The remaining
-- ~342 FGs still have no routing. The offline signal is a static CSV
-- (docs/.../Article_Master_FINAL.csv, gap_flags 403 / 238); this VIEW is the
-- ALWAYS-current, DB-side answer to "which FG bom_header rows still lack a
-- routing", so the resolution work can be driven (and verified) off live data.
--
--   * fg_routing_status VIEW: one row per bom_header, reporting whether it has
--     any bom_process_route steps. route_step_count is a correlated COUNT over
--     bom_process_route (LEFT-JOIN-equivalent — a bom_header with zero steps
--     still appears, with count 0). has_routing / needs_routing are the boolean
--     flags derived from that count. promoted_from_gap is surfaced so a reviewer
--     can see which already-handled rows the Slice-7 promotion touched.
--
-- NO new index is needed: bom_process_route(bom_id) is already covered by
-- idx_bom_route_bom (production_schema.sql), which serves the per-bom_id count;
-- adding another would just duplicate it. Adds NO columns to any table — a
-- derived view only.
--
-- Idempotent, forward-only. Reversible via app/db/rollback/sfg_integration_down.sql.
-- (dep: bom_header + bom_process_route from production_schema.sql;
--  bom_header.process_category from production_migrate.sql "Migration 1";
--  bom_header.promoted_from_gap from 058_fg_master_gap_promotion.sql)
-- ===========================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- fg_routing_status — one row per FG bom_header, flagging whether it has a
-- routing. The LEFT JOIN + GROUP BY keeps unrouted bom_header rows in the result
-- (route_step_count = 0), which are exactly the rows the gap-resolution feature
-- still has to close. has_routing / needs_routing are complementary booleans
-- derived from route_step_count. This is the durable "needs-routing" signal.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW fg_routing_status AS
SELECT
    h.bom_id,
    h.fg_sku_name,
    h.entity,
    h.process_category,
    h.promoted_from_gap,
    COUNT(r.route_id)            AS route_step_count,
    (COUNT(r.route_id) > 0)      AS has_routing,
    (COUNT(r.route_id) = 0)      AS needs_routing
FROM bom_header h
LEFT JOIN bom_process_route r
       ON r.bom_id = h.bom_id
GROUP BY
    h.bom_id,
    h.fg_sku_name,
    h.entity,
    h.process_category,
    h.promoted_from_gap;

COMMENT ON VIEW fg_routing_status IS
    'Live routing-gap signal: one row per FG bom_header with route_step_count '
    '(count of its bom_process_route steps) and the derived has_routing / '
    'needs_routing booleans. needs_routing = TRUE are the FGs that still lack a '
    'routing (no Process Category route) — the DB-side complement to the offline '
    'Article_Master_FINAL.csv gap list. Derived, not stored.';

COMMIT;
