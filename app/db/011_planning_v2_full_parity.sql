-- =========================================================================
-- 011_planning_v2_full_parity.sql
-- Re-expands so_fulfillment_v2 surface to match v1's feature set so the
-- frontend can drop v1 entirely. Restores carryforward (dropped in 009),
-- adds cancellation columns, adds 'cancel' to the revision-log enum, and
-- creates v2 versions of fulfillment_bom_override and fulfillment_floor_stock
-- with FKs to so_fulfillment_v2.
--
-- Idempotent. Safe to re-run. PostgreSQL ≥ 12.
-- =========================================================================

-- ----------------------------------------------------------------------
-- 1) Restore carryforward on so_fulfillment_v2
-- ----------------------------------------------------------------------

-- Re-add carryforward_from_id (self-ref FK to prior FY's row)
ALTER TABLE so_fulfillment_v2
    ADD COLUMN IF NOT EXISTS carryforward_from_id BIGINT
        REFERENCES so_fulfillment_v2(so_fulfillment_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_sof_v2_carryforward_from
    ON so_fulfillment_v2(carryforward_from_id)
    WHERE carryforward_from_id IS NOT NULL;

-- Re-allow 'carryforward' in order_status. Postgres has no ALTER CHECK,
-- so DROP + ADD with the predictable default constraint name. Idempotent.
ALTER TABLE so_fulfillment_v2
    DROP CONSTRAINT IF EXISTS so_fulfillment_v2_order_status_check;
ALTER TABLE so_fulfillment_v2
    ADD CONSTRAINT so_fulfillment_v2_order_status_check
    CHECK (order_status IN ('open','partial','fulfilled','carryforward','cancelled'));

-- ----------------------------------------------------------------------
-- 2) Cancellation columns on so_fulfillment_v2
-- ----------------------------------------------------------------------
ALTER TABLE so_fulfillment_v2
    ADD COLUMN IF NOT EXISTS cancelled_by         TEXT,
    ADD COLUMN IF NOT EXISTS cancelled_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancellation_reason  TEXT;

-- ----------------------------------------------------------------------
-- 3) Extend so_revision_log_v2 with 'carryforward', 'cancel', 'bom_override'
-- ----------------------------------------------------------------------
ALTER TABLE so_revision_log_v2
    DROP CONSTRAINT IF EXISTS so_revision_log_v2_revision_type_check;
ALTER TABLE so_revision_log_v2
    ADD CONSTRAINT so_revision_log_v2_revision_type_check
    CHECK (revision_type IN ('qty_change','units_change','date_change',
                              'carryforward','cancel','bom_override'));

-- ----------------------------------------------------------------------
-- 4) fulfillment_bom_override_v2 — per-fulfillment BOM overrides
--    Mirrors v1 but FKs to so_fulfillment_v2.so_fulfillment_id.
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fulfillment_bom_override_v2 (
    override_id          BIGSERIAL PRIMARY KEY,
    so_fulfillment_id    BIGINT NOT NULL
                         REFERENCES so_fulfillment_v2(so_fulfillment_id)
                         ON DELETE CASCADE,
    -- bom_line_id is NULL for "added items" not in the original BOM
    bom_line_id          INT,
    material_sku_name    TEXT,
    quantity_per_unit    NUMERIC(15,3),
    loss_pct             NUMERIC(5,3),
    uom                  TEXT,
    godown               TEXT,
    is_removed           BOOLEAN NOT NULL DEFAULT FALSE,
    override_reason      TEXT,
    overridden_by        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Unique override per (fulfillment, bom_line) — added items (bom_line_id IS NULL) can repeat
CREATE UNIQUE INDEX IF NOT EXISTS idx_bom_override_v2_unique
    ON fulfillment_bom_override_v2(so_fulfillment_id, bom_line_id)
    WHERE bom_line_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bom_override_v2_fulfillment
    ON fulfillment_bom_override_v2(so_fulfillment_id);

-- updated_at trigger
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_bom_override_v2_touch'
    ) THEN
        CREATE TRIGGER trg_bom_override_v2_touch
            BEFORE UPDATE ON fulfillment_bom_override_v2
            FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
    END IF;
END$$;

-- ----------------------------------------------------------------------
-- 5) fulfillment_floor_stock_v2 — manual floor stock entries per fulfillment
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fulfillment_floor_stock_v2 (
    floor_stock_id       BIGSERIAL PRIMARY KEY,
    so_fulfillment_id    BIGINT NOT NULL
                         REFERENCES so_fulfillment_v2(so_fulfillment_id)
                         ON DELETE CASCADE,
    material_sku_name    TEXT NOT NULL,
    item_type            TEXT DEFAULT 'pm',         -- rm, pm
    quantity_kg          NUMERIC(15,3) NOT NULL,
    unit                 TEXT NOT NULL DEFAULT 'KG',  -- KG or NOS
    floor_location       TEXT NOT NULL,
    added_by             TEXT,
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_floor_stock_v2_fulfillment
    ON fulfillment_floor_stock_v2(so_fulfillment_id);
CREATE INDEX IF NOT EXISTS idx_floor_stock_v2_material
    ON fulfillment_floor_stock_v2(material_sku_name);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_floor_stock_v2_touch'
    ) THEN
        CREATE TRIGGER trg_floor_stock_v2_touch
            BEFORE UPDATE ON fulfillment_floor_stock_v2
            FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
    END IF;
END$$;

-- ----------------------------------------------------------------------
-- Documentation
-- ----------------------------------------------------------------------
COMMENT ON COLUMN so_fulfillment_v2.carryforward_from_id IS
    'Self-ref FK to prior FY''s row when this row is created via FY rollover.';
COMMENT ON COLUMN so_fulfillment_v2.cancelled_by IS
    'User who cancelled the order. NULL unless order_status = ''cancelled''.';
COMMENT ON TABLE  fulfillment_bom_override_v2 IS
    'v2 per-fulfillment BOM-line overrides. NULL bom_line_id = added item not in master BOM.';
COMMENT ON TABLE  fulfillment_floor_stock_v2 IS
    'v2 manual floor-stock entries per fulfillment (material lying on production floor).';
