-- ===========================================================================
-- 058_fg_master_gap_promotion.sql  (SFG / Job-Card — Slice 7 data-quality)
-- ---------------------------------------------------------------------------
-- Slice 7 closes the article/routing reconciliation gaps so more SKUs can spawn
-- routed job cards. The two gaps the playbook targets are (counts from
-- docs/flows/.../Article_Master_FINAL.csv, gap_flags column):
--   * 403  CATALOG_FG_NOT_IN_FG_MASTER  — an all_sku FG with no Process Category,
--          so it has no bom_process_route → never enters the SFG/job-card build.
--   * 238  BOM_PARENT_NO_ROUTING        — a BOM parent (recipe exists) with no
--          Process Category / routing.
--
-- The FIX is DATA, not schema: promote a gap article into "FG Master" = upsert a
-- bom_header row carrying a `process_category` and derive its bom_process_route
-- steps (exactly what master_ingest.ingest_fg_master does for the FG_Master xlsx).
-- That data load is done by scripts/promote_fg_master_gaps.py (it needs the
-- article list from the CSV; it cannot live in static SQL).
--
-- This migration only adds the AUDIT marker the data script stamps, so the
-- promotion is identifiable and cleanly reversible (Slice 7 review gate: "data
-- cleanup is reversible/audited"). The column is NULLable + defaulted, so it is a
-- pure additive change with no effect on any existing row or query.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). Reversible via
-- app/db/rollback/sfg_integration_down.sql.
-- (dep: bom_header from production_schema.sql; process_category col from
--  production_migrate.sql "Migration 1")
-- ===========================================================================
BEGIN;

-- Audit flag: TRUE on a bom_header row created/updated by the Slice-7 gap
-- promotion (scripts/promote_fg_master_gaps.py). Lets a reviewer list exactly
-- what the promotion touched, and lets the data load be undone in isolation:
--     DELETE FROM bom_process_route r USING bom_header h
--       WHERE r.bom_id = h.bom_id AND h.promoted_from_gap;
--     DELETE FROM bom_header WHERE promoted_from_gap;   -- net-new rows only
-- (rows that pre-existed and were only ROUTED keep promoted_from_gap = TRUE but
--  must be unrouted, not deleted — the script logs which were net-new.)
ALTER TABLE bom_header
    ADD COLUMN IF NOT EXISTS promoted_from_gap BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN bom_header.promoted_from_gap IS
    'TRUE on rows created/routed by the Slice-7 reconciliation gap promotion '
    '(scripts/promote_fg_master_gaps.py). Audit marker; no behavioural effect.';

-- Partial index so "show me everything the promotion touched" is cheap, and so
-- the reversal DELETE above does not seq-scan bom_header.
CREATE INDEX IF NOT EXISTS idx_bom_header_promoted_from_gap
    ON bom_header(promoted_from_gap) WHERE promoted_from_gap;

COMMIT;
