-- 045_sample_inventory_accounting.sql  (NPD plan §1 — inventory accounting backbone)
-- Step B (FG-sample receipt) mints a finished trial sample into a dedicated R&D
-- stock location. inventory_batch.warehouse_id is free-text (no master table),
-- so the R&D location is the constant 'R&D' in sample_inventory_service.py — no
-- DDL needed for the location itself. This migration adds:
--   1. a place to record the minted FG-sample batch id on the two job-card homes;
--   2. the silent required-vs-issued consumption variance table (Step A), with
--      nullable cost columns pre-wired for the costing workstream (never exposed
--      to shop-floor roles).

ALTER TABLE npd_dev_job_cards
    ADD COLUMN IF NOT EXISTS fg_sample_batch_id TEXT;

ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS fg_sample_batch_id TEXT;

CREATE TABLE IF NOT EXISTS sample_consumption_variance (
    id                BIGSERIAL PRIMARY KEY,
    source_type       TEXT NOT NULL,            -- 'SAMPLE_REQ' | 'NPD_DEV_JC'
    source_id         INT  NOT NULL,
    material_sku_name TEXT NOT NULL,
    required_qty      NUMERIC(15, 3) NOT NULL,
    issued_qty        NUMERIC(15, 3) NOT NULL,
    variance_qty      NUMERIC(15, 3) GENERATED ALWAYS AS (issued_qty - required_qty) STORED,
    uom               TEXT NOT NULL DEFAULT 'kg',
    -- Pre-wired for the future costing/commercial workstream — nullable now,
    -- no approval flow; never serialised to shop-floor roles.
    unit_cost         NUMERIC(15, 4),
    variance_cost     NUMERIC(15, 2),
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sample_cons_var_src ON sample_consumption_variance(source_type, source_id);
