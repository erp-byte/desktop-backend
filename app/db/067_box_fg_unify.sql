-- ===========================================================================
-- 067_box_fg_unify.sql — unify sfg_box to hold BOTH kinds of physical units:
--   item_type = 'sfg' → boxes produced at a WIP / intermediate (Create-WIP) stage
--   item_type = 'fg'  → cartons produced at the terminal PACKING stage
--
-- Reconciles the 053 shape (box_id PK + box_number/total_boxes/lot_number) to the
-- new shape:
--   * box_id            renamed → carton_id   (still the 8-digit QR payload PK)
--   * box_number / total_boxes / lot_number   DROPPED
--   * added: item_type, batch_id, batch_code, units, fg_sku_name, so_number, created_by
--
-- Idempotent + safe on a fresh DB, on a 053-shape DB, and on a re-run.
-- ===========================================================================

-- Fresh-DB create in the FINAL shape (no-op when 053 already made the table).
CREATE TABLE IF NOT EXISTS sfg_box (
    carton_id                 BIGINT PRIMARY KEY,               -- QR payload (8-digit app id)
    item_type                 TEXT   NOT NULL DEFAULT 'sfg',    -- 'sfg' | 'fg'
    job_card_id               BIGINT NOT NULL,                  -- producing JC (WIP for sfg, packing for fg)
    job_card_number           TEXT,
    sfg_code                  TEXT   NOT NULL,                  -- generic item code (SFG#### | FG article code)
    fg_sku_name               TEXT,                             -- FG only (sticker / list)
    entity                    TEXT,
    floor                     TEXT,
    stage_bucket              TEXT,                             -- 'Create WIP' (sfg) | 'Packing' (fg)
    batch_id                  BIGINT,                           -- job_card_batch_v2 (FG cartons are batchwise)
    batch_code                TEXT,                             -- human-readable batch / MFG code
    net_weight                NUMERIC(15,3) NOT NULL,
    gross_weight              NUMERIC(15,3),
    units                     INT,                              -- FG units in this carton (NULL for SFG)
    status                    TEXT   NOT NULL DEFAULT 'PRINTED',
    source_inventory_batch_id TEXT,
    received_into_job_card_id BIGINT,
    parent_box_id             BIGINT,                           -- self-ref to carton_id (re-box genealogy)
    so_number                 TEXT,
    created_by                TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reconcile an existing 053-shape table → the new shape (each step guarded so a
-- fresh build, a 053 DB, and a re-run all converge without error).
DO $$
BEGIN
    -- PK rename box_id → carton_id (only if the old column is still present).
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'sfg_box' AND column_name = 'box_id') THEN
        ALTER TABLE sfg_box RENAME COLUMN box_id TO carton_id;
    END IF;

    -- New columns (DEFAULT 'sfg' backfills existing rows as SFG boxes).
    ALTER TABLE sfg_box ADD COLUMN IF NOT EXISTS item_type   TEXT NOT NULL DEFAULT 'sfg';
    ALTER TABLE sfg_box ADD COLUMN IF NOT EXISTS batch_id    BIGINT;
    ALTER TABLE sfg_box ADD COLUMN IF NOT EXISTS batch_code  TEXT;
    ALTER TABLE sfg_box ADD COLUMN IF NOT EXISTS units       INT;
    ALTER TABLE sfg_box ADD COLUMN IF NOT EXISTS fg_sku_name TEXT;
    ALTER TABLE sfg_box ADD COLUMN IF NOT EXISTS so_number   TEXT;
    ALTER TABLE sfg_box ADD COLUMN IF NOT EXISTS created_by  TEXT;

    -- Drop the per-position columns + their dependent constraints.
    ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS uq_sfg_box_jc_boxnum;
    ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_sfg_box_number;
    ALTER TABLE sfg_box DROP COLUMN IF EXISTS box_number;
    ALTER TABLE sfg_box DROP COLUMN IF EXISTS total_boxes;
    ALTER TABLE sfg_box DROP COLUMN IF EXISTS lot_number;

    -- item_type domain.
    ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_box_item_type;
    ALTER TABLE sfg_box ADD  CONSTRAINT chk_box_item_type CHECK (item_type IN ('sfg','fg'));

    -- status domain now also allows CANCELLED.
    ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_sfg_box_status;
    ALTER TABLE sfg_box ADD  CONSTRAINT chk_sfg_box_status
        CHECK (status IN ('PRINTED','DISPATCHED','RECEIVED','CONSUMED','CANCELLED'));
END $$;

CREATE INDEX IF NOT EXISTS idx_sfg_box_jc    ON sfg_box(job_card_id);
CREATE INDEX IF NOT EXISTS idx_sfg_box_sku   ON sfg_box(sfg_code, status);
CREATE INDEX IF NOT EXISTS idx_sfg_box_recv  ON sfg_box(received_into_job_card_id);
CREATE INDEX IF NOT EXISTS idx_sfg_box_batch ON sfg_box(batch_id);
CREATE INDEX IF NOT EXISTS idx_sfg_box_type  ON sfg_box(item_type, status);

COMMENT ON TABLE sfg_box IS
  'Physical boxes/cartons. item_type=sfg -> WIP-stage boxes; item_type=fg -> '
  'packing-stage FG cartons (batchwise, with units). carton_id is the printed QR.';
