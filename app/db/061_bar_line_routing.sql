-- ===========================================================================
-- 061_bar_line_routing.sql   (SFG / Job-Card integration — G4 Bar-Line override)
-- ---------------------------------------------------------------------------
-- bom_header.bar_line_process (TEXT, added by 031_bom_bar_line_process.sql) is a
-- RICHER routing string than process_category — e.g.
--   "Receiving + Sorting + Weighing + Roasting + Mixing + Krugger + Flow +
--    X-ray + Monocarton + Master Carton"
-- It is filled for the ~84 bar-line value-added FGs but is currently consumed
-- NOWHERE: those FGs still derive their bom_process_route from the coarser
-- process_category. G4 ("Bar-Line Process override") lets those 84 FGs (re)build
-- their routing FROM bar_line_process instead.
--
-- The override itself is DATA, not schema: it REPLACES the candidate FGs'
-- bom_process_route steps with the ones parsed out of bar_line_process and
-- stamps the audit flag below (opt-in + idempotent — done by the parallel
-- backend agent's script, which needs to tokenise the routing string; that
-- cannot live in static SQL). This migration only adds the AUDIT marker the
-- script stamps, plus a candidate VIEW so a reviewer can see (and the script can
-- target) exactly which FGs carry a bar_line_process. Pure additive — NULLable-
-- equivalent (NOT NULL DEFAULT FALSE) column has no effect on any existing row.
--
--   * bom_header.bar_line_routed BOOLEAN — TRUE on an FG whose bom_process_route
--     was (re)built from bar_line_process by the G4 override (mirrors the 058
--     promoted_from_gap audit-marker pattern exactly).
--   * idx_bom_header_bar_line_routed — partial index so "show me everything the
--     override touched" is cheap and the reversal does not seq-scan bom_header.
--   * bar_line_fg VIEW — the candidate list: every bom_header that carries a
--     non-blank bar_line_process, with its bar_line_routed flag so a reviewer can
--     tell handled rows from pending ones. Derived, not stored.
--
-- Idempotent (ADD COLUMN / INDEX IF NOT EXISTS, CREATE OR REPLACE VIEW),
-- forward-only. Reversible via app/db/rollback/sfg_integration_down.sql.
-- (dep: bom_header from production_schema.sql; bar_line_process col from
--  031_bom_bar_line_process.sql; process_category col from production_migrate.sql
--  "Migration 1")
-- ===========================================================================
BEGIN;

-- Audit flag: TRUE on a bom_header FG whose bom_process_route was (re)built from
-- bar_line_process by the G4 Bar-Line override. Lets a reviewer list exactly what
-- the override touched, and lets the data load be undone in isolation. NULLable-
-- safe (NOT NULL DEFAULT FALSE) — additive, no behavioural effect.
ALTER TABLE bom_header
    ADD COLUMN IF NOT EXISTS bar_line_routed BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN bom_header.bar_line_routed IS
    'TRUE on FGs whose bom_process_route was (re)built from bar_line_process by '
    'the G4 Bar-Line override. Audit marker; no behavioural effect.';

-- Partial index so "show me everything the override touched" is cheap, and so
-- the reversal does not seq-scan bom_header.
CREATE INDEX IF NOT EXISTS idx_bom_header_bar_line_routed
    ON bom_header(bar_line_routed) WHERE bar_line_routed;

-- ---------------------------------------------------------------------------
-- bar_line_fg — the G4 candidate list: every bom_header carrying a non-blank
-- bar_line_process (the ~84 bar-line VA FGs). bar_line_routed shows which the
-- override has already (re)routed; process_category is surfaced alongside the
-- richer bar_line_process so a reviewer can compare the coarse vs. rich routing.
-- Derived, not stored.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW bar_line_fg AS
SELECT
    bom_id,
    fg_sku_name,
    entity,
    bar_line_process,
    process_category,
    bar_line_routed
FROM bom_header
WHERE bar_line_process IS NOT NULL
  AND btrim(bar_line_process) <> '';

COMMENT ON VIEW bar_line_fg IS
    'G4 candidate list: one row per bom_header FG carrying a non-blank '
    'bar_line_process (the ~84 bar-line VA FGs eligible for the Bar-Line routing '
    'override), with bar_line_routed flagging which have already been (re)routed. '
    'Derived, not stored.';

COMMIT;
