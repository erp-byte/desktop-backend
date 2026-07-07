-- ===========================================================================
-- 059_sfg_genealogy.sql   (SFG / Job-Card integration — Phase 7, DB layer)
-- ---------------------------------------------------------------------------
-- Activates the batch/lot GENEALOGY that Slice 6 deliberately DEFERRED. The
-- physical-traceability columns already exist on sfg_box (053): the nullable
-- `lot_number` (= the source WIP batch's lot, WLOT-{batch_id}) and the BIGINT
-- self-reference `parent_box_id` (box→box across a 2nd WIP stage). The backend
-- (Slice-6 sfg_box_service, in parallel) now POPULATES those two columns; this
-- migration adds the read-side artifacts so the populated genealogy is queryable
-- and traceable: box → box (parent_box_id) → source WIP inventory_batch / lot.
--
-- This is the durable trace surface. The Slice-7 trace endpoints may query the
-- base tables directly for the live path; `sfg_genealogy` is the stable,
-- ad-hoc-queryable artifact (one flattened row per box joined to its provenance).
--
--   * Indexes (traversal): partial indexes on the genealogy join keys so box→box
--     and box→lot walks, and the downstream-JC trace, stay cheap. Partial (only
--     the rows that actually carry the key) — these columns are NULL for boxes
--     that have no parent / no lot. The (received_into_job_card_id) index is
--     ALREADY created by 053 (idx_sfg_box_recv) — NOT re-created here.
--   * sfg_genealogy VIEW: one row per sfg_box LEFT JOINed to its source WIP
--     inventory_batch, so a box with no source batch still appears.
--
-- Adds NO columns to sfg_box (053 already has them) — indexes + view only.
-- Idempotent, forward-only. Reversible via app/db/rollback/sfg_integration_down.sql.
-- (dep: 053 sfg_box; inventory_batch from production_schema; 057 inventory_batch.sfg_code)
-- ===========================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- Genealogy traversal indexes. Partial (only rows carrying the key) — cheap to
-- maintain, and the deferred columns are NULL on most rows. NOTE: the
-- (received_into_job_card_id) index already exists as idx_sfg_box_recv (053),
-- so it is intentionally NOT created here (no duplicate).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_sfg_box_parent
    ON sfg_box(parent_box_id) WHERE parent_box_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sfg_box_lot
    ON sfg_box(lot_number)    WHERE lot_number    IS NOT NULL;

-- ---------------------------------------------------------------------------
-- sfg_genealogy — one flattened row per physical SFG box joined to its
-- provenance (producing JC, downstream JC, parent box, source WIP batch + lot).
-- LEFT JOIN to inventory_batch so a box whose source batch is missing/unset
-- still appears (source_inventory_batch_id is sfg_box's TEXT ref to the WIP
-- inventory_batch.batch_id). This is the durable trace/reporting surface.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW sfg_genealogy AS
SELECT
    b.box_id,
    b.sfg_code,
    b.lot_number,
    b.parent_box_id,
    b.status,
    b.net_weight,
    b.job_card_id                AS producer_job_card_id,
    b.received_into_job_card_id,
    b.source_inventory_batch_id,
    -- provenance from the source WIP inventory_batch (LEFT JOIN: may be absent)
    ib.sku_name                  AS source_batch_sku_name,
    ib.lot_number                AS source_batch_lot,
    ib.entity                    AS source_batch_entity,
    ib.inward_date               AS source_batch_inward_date
FROM sfg_box b
LEFT JOIN inventory_batch ib
       ON ib.batch_id = b.source_inventory_batch_id;

COMMENT ON VIEW sfg_genealogy IS
    'Flattened SFG box genealogy: one row per sfg_box joined (LEFT) to its source '
    'WIP inventory_batch — box -> parent_box_id (box->box) -> source batch/lot. '
    'Activates the Slice-6-deferred lot_number/parent_box_id traceability. '
    'Durable trace/reporting artifact; derived, not stored.';

COMMIT;
