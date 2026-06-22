-- ===========================================================================
-- sfg_integration_down.sql   (REVERSAL of the SFG / Job-Card DB migrations)
-- ---------------------------------------------------------------------------
-- The repo is FORWARD-ONLY (no down-migrations are wired into scripts/migrate.py).
-- This script is therefore NOT in SQL_FILES and never auto-runs — it is a manual
-- teardown you invoke explicitly to undo the SFG schema footprint:
--
--     psql "$DATABASE_URL" -f app/db/rollback/sfg_integration_down.sql
--
-- It reverses, in REVERSE-CHRONOLOGICAL order (so dependents go before the
-- objects they depend on), the DDL added by:
--     062_sfg_seam_pending.sql      (sfg_seam_pending view — Phase 9 SFG-seam
--                                    work-queue/audit; + idx_bom_route_seam_pending
--                                    partial index. Derived view + index only)
--     061_bar_line_routing.sql      (bom_header.bar_line_routed audit column +
--                                    partial index; bar_line_fg candidate view —
--                                    the G4 Bar-Line override schema/audit layer)
--     060_routing_gap_status.sql    (fg_routing_status view — live routing-gap
--                                    signal; derived view only, no columns/indexes)
--     059_sfg_genealogy.sql         (sfg_genealogy view + sfg_box parent/lot
--                                    traversal indexes — Slice-6-deferred genealogy)
--     057_sfg_inventory_key_and_attrs.sql (inventory_batch.sfg_code + index;
--                                    sfg_attributes.va_article/primary_bu;
--                                    sfg_master re-def — those cols/view drop below)
--     056_sfg_master_enrichment.sql (sfg_attributes/stage_catalog/fg_sfg_input_map/
--                                    sfg_resolution_map tables; sfg_master re-def)
--     055_sfg_catalogue_views.sql   (views + reverse SFG#### indexes)
--     053_sfg_box.sql               (sfg_box table)
--     052_sfg_routing_bom.sql       (documentary COMMENTs only — cosmetic)
--     051_sfg_seed_catalog.sql      (all_sku.sfg_code + index; sku_id widening)
--     050_sfg_foundation.sql        (seam columns on bom_process_route / bom_line
--                                    / job_card_v2; documentary COMMENTs)
--
-- SCOPE: this reverses SCHEMA (the "db commands"). Master DATA loaded at app
-- startup by the Python ingests (SFG rows in all_sku, routing seam stamps in
-- bom_process_route, sfg bom_lines, sfg_box rows) is handled in the clearly
-- marked OPTIONAL DATA CLEANUP section at the end — left commented because it is
-- destructive and, for the re-typed (not net-new) all_sku rows, lossy. Read it
-- before uncommenting.
--
-- Idempotent: every drop is IF EXISTS; safe to re-run / partial-run.
-- ===========================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- (062) SFG seam-minting observability (Phase 9) — the sfg_seam_pending work-
-- queue/audit view + its partial index idx_bom_route_seam_pending. Dropped FIRST
-- (reverse-chrono — 062 is newest). The view is derived over bom_process_route +
-- bom_header (altered/owned well below); the index is on the bom_process_route
-- NULL-output Create-WIP predicate. The migration added NO columns, so the view +
-- index drop is the entire reversal. The seam stamps the minting script writes
-- are DATA, undone separately (the routing-seam UPDATE in the OPTIONAL DATA
-- CLEANUP block clears output_code et al). Idempotent.
-- ---------------------------------------------------------------------------
DROP VIEW  IF EXISTS sfg_seam_pending;
DROP INDEX IF EXISTS idx_bom_route_seam_pending;

-- ---------------------------------------------------------------------------
-- (061) Bar-Line Process override (G4) — the bar_line_fg candidate view +
-- bom_header.bar_line_routed audit column + its partial index. Dropped FIRST
-- (reverse-chrono — 061 is newest). The view is dropped before the column it
-- selects (bar_line_routed); the column + partial index are the only schema the
-- migration added (the override's route REPLACEMENT is DATA, undone separately —
-- see the OPTIONAL DATA CLEANUP note at the end). Idempotent.
-- ---------------------------------------------------------------------------
DROP VIEW  IF EXISTS bar_line_fg;
DROP INDEX IF EXISTS idx_bom_header_bar_line_routed;
ALTER TABLE bom_header DROP COLUMN IF EXISTS bar_line_routed;

-- ---------------------------------------------------------------------------
-- (060) Routing-Gap status view — the live fg_routing_status signal (one row per
-- FG bom_header with route_step_count + has_routing / needs_routing). Dropped
-- FIRST (reverse-chrono — 060 is newest). It is a derived view over bom_header +
-- bom_process_route (both dropped/altered well below / owned by base schema); the
-- view added NO columns and NO new indexes (060 reused idx_bom_route_bom), so the
-- view drop is the entire reversal. Idempotent.
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS fg_routing_status;

-- ---------------------------------------------------------------------------
-- (059) SFG genealogy view + traversal indexes (Phase 7 — the Slice-6-deferred
-- batch/lot traceability). Dropped FIRST (reverse-chrono — 059 is newest). The
-- view depends on sfg_box + inventory_batch (dropped/altered further below); the
-- two partial indexes are on sfg_box columns (053) that 053 itself created — only
-- the indexes ADDED by 059 are dropped here (idx_sfg_box_recv is 053's, left for
-- the sfg_box table drop in the 053 step). No sfg_box columns are touched (053
-- owns them). Idempotent.
-- ---------------------------------------------------------------------------
DROP VIEW  IF EXISTS sfg_genealogy;
DROP INDEX IF EXISTS idx_sfg_box_parent;
DROP INDEX IF EXISTS idx_sfg_box_lot;

-- ---------------------------------------------------------------------------
-- (058) bom_header.promoted_from_gap audit marker + its partial index.
-- This reverses ONLY the schema column (the "db command"). The gap-promotion
-- DATA (net-new bom_header rows + their bom_process_route steps) is master data
-- loaded by scripts/promote_fg_master_gaps.py and is purged in the OPTIONAL DATA
-- CLEANUP block at the end (left commented — it is destructive, and for rows that
-- pre-existed and were only ROUTED it is lossy). Dropped first (reverse-chrono).
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS idx_bom_header_promoted_from_gap;
ALTER TABLE bom_header DROP COLUMN IF EXISTS promoted_from_gap;

-- ---------------------------------------------------------------------------
-- (057) inventory_batch.sfg_code + index (the sfg_attributes va_article/primary_bu
-- columns drop with the table in the 056 step; the sfg_master re-def drops with the
-- view in the 055 step). inventory_batch is a base table — only the added column goes.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS idx_inv_batch_sfg_code;
ALTER TABLE inventory_batch DROP COLUMN IF EXISTS sfg_code;

-- ---------------------------------------------------------------------------
-- (055) catalogue views + reverse SFG#### indexes.
-- Views first: sfg_where_used depends on sfg_master, and the indexes reference
-- bom_process_route.input_code/output_code (dropped below in the 050 step).
-- ---------------------------------------------------------------------------
DROP VIEW  IF EXISTS sfg_where_used;
DROP VIEW  IF EXISTS fg_sfg_binding;
DROP VIEW  IF EXISTS sfg_master;
DROP INDEX IF EXISTS idx_bom_route_input_code;
DROP INDEX IF EXISTS idx_bom_route_output_code;

-- ---------------------------------------------------------------------------
-- (056) enrichment / reconciliation tables. Dropped AFTER the views above
-- (sfg_master's 056 re-definition LEFT JOINs sfg_attributes). Each table's own
-- indexes/constraints drop with it.
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS sfg_resolution_map;
DROP TABLE IF EXISTS fg_sfg_input_map;
DROP TABLE IF EXISTS stage_catalog;
DROP TABLE IF EXISTS sfg_attributes;

-- ---------------------------------------------------------------------------
-- (053) sfg_box table (standalone; its indexes/constraints drop with it).
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS sfg_box;

-- ---------------------------------------------------------------------------
-- (052) was documentary only (COMMENT ON COLUMN on the bom_process_route seam
-- columns). No action needed: those comments are dropped automatically with
-- their columns in the (050) step below. (Explicitly resetting them here would
-- break idempotency — on a re-run the columns are already gone.)
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- (051) all_sku SFG catalogue schema.
--   - drop the partial unique index + the sfg_code business key.
--   - OPTIONAL: narrow sku_id BIGINT -> INTEGER again. Guarded so it only fires
--     when every existing id fits int4 (all 8-digit app-supplied ids do). This
--     is left ENABLED because it is safe under that guard; comment it out if you
--     prefer to keep the column BIGINT (widening is itself harmless).
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS uq_all_sku_sfg_code;
ALTER TABLE all_sku DROP COLUMN IF EXISTS sfg_code;

DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
         WHERE table_name = 'all_sku' AND column_name = 'sku_id') = 'bigint'
       AND NOT EXISTS (SELECT 1 FROM all_sku WHERE sku_id > 2147483647) THEN
        ALTER TABLE all_sku ALTER COLUMN sku_id TYPE INTEGER;
    ELSE
        RAISE NOTICE 'sfg rollback: sku_id left as-is (already int, or holds an id > int4 max)';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- (050) seam columns + Option-B bom_line column + guarded job_card_v2 codes.
-- These are dropped LAST: the 055 views/indexes above referenced input_code /
-- output_code, so they must already be gone (they are).
-- ---------------------------------------------------------------------------
ALTER TABLE bom_process_route DROP COLUMN IF EXISTS practical_operation;
ALTER TABLE bom_process_route DROP COLUMN IF EXISTS stage_bucket;
ALTER TABLE bom_process_route DROP COLUMN IF EXISTS input_kind;
ALTER TABLE bom_process_route DROP COLUMN IF EXISTS output_kind;
ALTER TABLE bom_process_route DROP COLUMN IF EXISTS input_code;
ALTER TABLE bom_process_route DROP COLUMN IF EXISTS output_code;

ALTER TABLE bom_line DROP COLUMN IF EXISTS consumed_at_stage;

-- job_card_v2 is orphaned in the migrate.py runner (created by 017) — guard the
-- drop exactly as 050 guards the add.
DO $$
BEGIN
    IF to_regclass('job_card_v2') IS NOT NULL THEN
        ALTER TABLE job_card_v2 DROP COLUMN IF EXISTS input_code;
        ALTER TABLE job_card_v2 DROP COLUMN IF EXISTS output_code;
    END IF;
END $$;

-- Reset the 050 documentary COMMENTs on item_type (cosmetic).
COMMENT ON COLUMN all_sku.item_type  IS NULL;
COMMENT ON COLUMN bom_line.item_type IS NULL;

COMMIT;

-- ===========================================================================
-- OPTIONAL DATA CLEANUP  (DESTRUCTIVE — review before uncommenting)
-- ---------------------------------------------------------------------------
-- The schema reversal above leaves master DATA in place. To also purge the data
-- the Python ingests loaded, run the block below. NOTE the asymmetry:
--   * Net-new SFG rows carry an 8-digit app-supplied sku_id (>= 10000000) and can
--     be deleted cleanly.
--   * Some all_sku rows were RE-TYPED in place to item_type='sfg' (their prior
--     item_type was logged by ingest_sfg_master). Those cannot be auto-restored
--     here — restore them from that log if needed BEFORE deleting net-new rows.
--   * sfg_box / WIP inventory_batch (item_type='wip') / floor_movement audit rows
--     produced at runtime are transactional, not master data — purge only if you
--     are resetting a test DB.
--
-- BEGIN;
--   -- runtime WIP materialisation (test reset only):
--   -- DELETE FROM sfg_box;
--   -- DELETE FROM inventory_batch WHERE item_type = 'wip';
--   -- routing seam stamps (keep the routing rows; just clear the SFG stamps):
--   -- UPDATE bom_process_route
--   --    SET practical_operation = NULL, stage_bucket = NULL,
--   --        input_kind = NULL, output_kind = NULL,
--   --        input_code = NULL, output_code = NULL;
--   -- Option-B sfg bom_lines:
--   -- DELETE FROM bom_line WHERE item_type = 'sfg';
--   -- net-new SFG catalogue rows (8-digit app-supplied ids):
--   -- DELETE FROM all_sku WHERE item_type = 'sfg' AND sku_id >= 10000000;
--   -- (re-typed rows: restore prior item_type from the ingest_sfg_master log,
--   --  then this line would also clear any remaining sfg rows if desired)
-- COMMIT;
--
-- ---------------------------------------------------------------------------
-- (058) Slice-7 reconciliation gap-promotion DATA cleanup.
-- IMPORTANT: this keys on bom_header.promoted_from_gap, which the SCHEMA reversal
-- above DROPS. Run THIS block FIRST (before the schema reversal at the top of the
-- file) if you want to undo the promotion's data, OR re-add the column, run it,
-- then drop the column again. The script tags BOTH net-new rows AND pre-existing
-- rows it merely ROUTED with promoted_from_gap=TRUE — only the net-new ones are
-- safe to DELETE; the routed-only ones must just have their route cleared.
-- (scripts/promote_fg_master_gaps.py logs which bom_ids were net-new.)
--
-- BEGIN;
--   -- routes added by the promotion (safe for BOTH net-new and routed-only rows):
--   -- DELETE FROM bom_process_route r USING bom_header h
--   --   WHERE r.bom_id = h.bom_id AND h.promoted_from_gap;
--   -- net-new bom_header rows ONLY (the script logs these bom_ids; do NOT delete
--   -- a row that pre-existed and was only routed):
--   -- DELETE FROM bom_header WHERE promoted_from_gap AND <net-new bom_id list>;
-- COMMIT;
-- ===========================================================================
