-- Production Planning Module migrations (v1)
-- Idempotent ALTER TABLE statements go here as schema evolves.
-- Pattern: DO $$ BEGIN ... EXCEPTION WHEN ... END $$;

-- Migration 1: Add FG Master columns to bom_header
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bom_header' AND column_name='sub_group') THEN
        ALTER TABLE bom_header ADD COLUMN sub_group TEXT;
        ALTER TABLE bom_header ADD COLUMN process_category TEXT;
        ALTER TABLE bom_header ADD COLUMN business_unit TEXT;
        ALTER TABLE bom_header ADD COLUMN factory TEXT;
        ALTER TABLE bom_header ADD COLUMN floors TEXT[];
        ALTER TABLE bom_header ADD COLUMN machines TEXT[];
        ALTER TABLE bom_header ADD COLUMN shelf_life_days INT;
        ALTER TABLE bom_header ADD COLUMN gst_rate NUMERIC(5,3);
        ALTER TABLE bom_header ADD COLUMN hsn_sac TEXT;
        ALTER TABLE bom_header ADD COLUMN inventory_group TEXT;
        ALTER TABLE bom_header ADD COLUMN customer_code TEXT;
    END IF;
END
$$;

-- Migration 2: Add enrichment columns to bom_line
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bom_line' AND column_name='unit_rate_inr') THEN
        ALTER TABLE bom_line ADD COLUMN unit_rate_inr NUMERIC(15,3);
        ALTER TABLE bom_line ADD COLUMN process_stage TEXT;
    END IF;
END
$$;

-- Migration 3: Add allocation column to machine
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='machine' AND column_name='allocation') THEN
        ALTER TABLE machine ADD COLUMN allocation TEXT NOT NULL DEFAULT 'idle';
    END IF;
END
$$;

-- Migration 4: Add PDF fields to job_card (Section 1 & 3 of CFC/PRD/JC/V3.0)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='job_card' AND column_name='sales_order_ref') THEN
        ALTER TABLE job_card ADD COLUMN sales_order_ref TEXT;
        ALTER TABLE job_card ADD COLUMN article_code TEXT;
        ALTER TABLE job_card ADD COLUMN mrp NUMERIC(15,3);
        ALTER TABLE job_card ADD COLUMN ean TEXT;
        ALTER TABLE job_card ADD COLUMN bu TEXT;
        ALTER TABLE job_card ADD COLUMN fumigation BOOLEAN DEFAULT FALSE;
        ALTER TABLE job_card ADD COLUMN metal_detector_used BOOLEAN DEFAULT FALSE;
        ALTER TABLE job_card ADD COLUMN roasting_pasteurization BOOLEAN DEFAULT FALSE;
        ALTER TABLE job_card ADD COLUMN control_sample_gm NUMERIC(10,2);
        ALTER TABLE job_card ADD COLUMN magnets_used BOOLEAN DEFAULT FALSE;
    END IF;
END
$$;

-- Migration 5: Add Annexure A/B fields to job_card_metal_detection
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='job_card_metal_detection' AND column_name='seal_check') THEN
        ALTER TABLE job_card_metal_detection ADD COLUMN seal_check BOOLEAN;
        ALTER TABLE job_card_metal_detection ADD COLUMN seal_failed_units INT DEFAULT 0;
        ALTER TABLE job_card_metal_detection ADD COLUMN wt_check BOOLEAN;
        ALTER TABLE job_card_metal_detection ADD COLUMN wt_failed_units INT DEFAULT 0;
        ALTER TABLE job_card_metal_detection ADD COLUMN dough_temp_c NUMERIC(10,2);
        ALTER TABLE job_card_metal_detection ADD COLUMN oven_temp_c NUMERIC(10,2);
        ALTER TABLE job_card_metal_detection ADD COLUMN baking_temp_c NUMERIC(10,2);
    END IF;
END
$$;

-- Migration 6: Add Annexure B fields to job_card_weight_check
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='job_card_weight_check' AND column_name='target_wt_g') THEN
        ALTER TABLE job_card_weight_check ADD COLUMN target_wt_g NUMERIC(10,2);
        ALTER TABLE job_card_weight_check ADD COLUMN tolerance_g NUMERIC(10,2);
        ALTER TABLE job_card_weight_check ADD COLUMN accept_range_min NUMERIC(10,2);
        ALTER TABLE job_card_weight_check ADD COLUMN accept_range_max NUMERIC(10,2);
    END IF;
END
$$;

-- Migration 7: Create job_card_sign_off table (Section 6 + Annexure page 4)
CREATE TABLE IF NOT EXISTS job_card_sign_off (
    sign_off_id     SERIAL PRIMARY KEY,
    job_card_id     INT NOT NULL REFERENCES job_card(job_card_id),
    sign_off_type   TEXT NOT NULL,
    name            TEXT,
    signed_at       TIMESTAMPTZ,
    UNIQUE (job_card_id, sign_off_type)
);

CREATE INDEX IF NOT EXISTS idx_sign_off_jc ON job_card_sign_off(job_card_id);

-- Migration 8: Add output_uom to bom_header and uom to floor_inventory
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='bom_header' AND column_name='output_uom') THEN
        ALTER TABLE bom_header ADD COLUMN output_uom TEXT DEFAULT 'kg';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='floor_inventory' AND column_name='uom') THEN
        ALTER TABLE floor_inventory ADD COLUMN uom TEXT DEFAULT 'kg';
    END IF;
END
$$;

-- Migration 9: Per-fulfillment BOM override table
CREATE TABLE IF NOT EXISTS fulfillment_bom_override (
    override_id         SERIAL PRIMARY KEY,
    fulfillment_id      INT NOT NULL REFERENCES so_fulfillment(fulfillment_id),
    bom_line_id         INT NOT NULL REFERENCES bom_line(bom_line_id),
    material_sku_name   TEXT,              -- NULL = use master value
    quantity_per_unit   NUMERIC(15,3),     -- NULL = use master value
    loss_pct            NUMERIC(5,3),      -- NULL = use master value
    uom                 TEXT,
    godown              TEXT,
    is_removed          BOOLEAN NOT NULL DEFAULT FALSE,
    override_reason     TEXT,
    overridden_by       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (fulfillment_id, bom_line_id)
);

CREATE INDEX IF NOT EXISTS idx_bom_override_fulfillment ON fulfillment_bom_override(fulfillment_id);

-- Allow added items (not in original BOM) to have NULL bom_line_id
ALTER TABLE fulfillment_bom_override ALTER COLUMN bom_line_id DROP NOT NULL;
-- Drop the FK constraint so NULL and added-item rows work
ALTER TABLE fulfillment_bom_override DROP CONSTRAINT IF EXISTS fulfillment_bom_override_bom_line_id_fkey;
-- Drop the old unique constraint and replace with one that allows multiple NULL bom_line_ids
ALTER TABLE fulfillment_bom_override DROP CONSTRAINT IF EXISTS fulfillment_bom_override_fulfillment_id_bom_line_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_bom_override_unique
    ON fulfillment_bom_override(fulfillment_id, bom_line_id) WHERE bom_line_id IS NOT NULL;

-- ═══════════════════════════════════════════════════════════════
-- Store Control: Material Allocation with Approval/Rejection
-- ═══════════════════════════════════════════════════════════════

-- Central audit trail of every store decision per indent line
CREATE TABLE IF NOT EXISTS store_allocation (
    allocation_id               SERIAL PRIMARY KEY,
    job_card_id                 INT NOT NULL REFERENCES job_card(job_card_id),
    indent_type                 TEXT NOT NULL,                          -- 'rm' or 'pm'
    indent_id                   INT NOT NULL,                          -- rm_indent_id or pm_indent_id
    material_sku_name           TEXT NOT NULL,
    reqd_qty                    NUMERIC(15,3) NOT NULL,
    approved_qty                NUMERIC(15,3) DEFAULT 0,
    rejected_qty                NUMERIC(15,3) DEFAULT 0,
    decision                    TEXT NOT NULL DEFAULT 'pending',       -- pending, approved, partial, rejected, alternative_offered
    rejection_reason            TEXT,                                   -- reserved_for_customer, quality_mismatch, expired, insufficient_stock, other
    rejection_detail            TEXT,
    reserved_for_customer       TEXT,
    quality_grade_available     TEXT,
    quality_grade_required      TEXT,
    expiry_date                 DATE,
    suggested_alternative_id    INT,                                    -- offgrade_inventory.offgrade_id
    suggested_alternative_qty   NUMERIC(15,3),
    purchase_indent_id          INT,                                    -- purchase_indent.indent_id (if raised)
    floor_stock_verified        BOOLEAN DEFAULT FALSE,
    floor_stock_qty             NUMERIC(15,3),
    source_location             TEXT,                                   -- rm_store, pm_store, production_floor
    decided_by                  TEXT,
    decided_at                  TIMESTAMPTZ,
    entity                      TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_store_alloc_jc ON store_allocation(job_card_id);
CREATE INDEX IF NOT EXISTS idx_store_alloc_decision ON store_allocation(decision);
CREATE INDEX IF NOT EXISTS idx_store_alloc_entity ON store_allocation(entity);

-- Add store decision columns to indent tables
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS store_decision TEXT DEFAULT 'pending';
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS store_approved_qty NUMERIC(15,3) DEFAULT 0;
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS store_decided_by TEXT;
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS store_decided_at TIMESTAMPTZ;
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS source_location TEXT;
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS quality_grade TEXT;

ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS store_decision TEXT DEFAULT 'pending';
ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS store_approved_qty NUMERIC(15,3) DEFAULT 0;
ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS store_decided_by TEXT;
ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS store_decided_at TIMESTAMPTZ;
ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS source_location TEXT;
ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS quality_grade TEXT;

-- Add store allocation status to job card
ALTER TABLE job_card ADD COLUMN IF NOT EXISTS store_allocation_status TEXT DEFAULT 'pending';

-- Add store-rejection tracking to purchase indent
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS store_allocation_id INT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS job_card_id INT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS indent_source TEXT DEFAULT 'mrp';

-- Manual acknowledgement audit columns (fallback to QR scanning)
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS manual_ack_by TEXT;
ALTER TABLE job_card_rm_indent ADD COLUMN IF NOT EXISTS manual_ack_at TIMESTAMPTZ;
ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS manual_ack_by TEXT;
ALTER TABLE job_card_pm_indent ADD COLUMN IF NOT EXISTS manual_ack_at TIMESTAMPTZ;

-- Inventory-Production integration: enhanced purchase indent fields
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS customer_name TEXT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS so_reference TEXT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS triggered_by_batch TEXT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS shortfall_qty_kg NUMERIC(15,3);
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS cascade_from_indent_id INT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS cascade_reason TEXT;

-- Data migration: populate inventory_batch from existing po_box records
INSERT INTO inventory_batch (batch_id, sku_name, item_type, transaction_no, lot_number,
    source, inward_date, original_qty_kg, current_qty_kg, warehouse_id, floor_id, entity)
SELECT
    b.box_id,
    l.sku_name,
    'rm',
    b.transaction_no,
    b.lot_number,
    'INWARD',
    COALESCE(h.po_date::date, h.created_at::date, CURRENT_DATE),
    COALESCE(b.net_weight, b.gross_weight, 0),
    COALESCE(b.net_weight, b.gross_weight, 0),
    h.warehouse,
    'rm_store',
    COALESCE(h.entity, 'cfpl')
FROM po_box b
JOIN po_line l ON b.transaction_no = l.transaction_no AND b.line_number = l.line_number
JOIN po_header h ON b.transaction_no = h.transaction_no
WHERE COALESCE(b.net_weight, b.gross_weight, 0) > 0
ON CONFLICT (batch_id) DO NOTHING;

-- Batch rejection log (FIFO skip reasons)
CREATE TABLE IF NOT EXISTS batch_rejection_log (
    log_id          SERIAL PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    rejected_by     TEXT NOT NULL,
    rejected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason_code     TEXT NOT NULL,          -- QUALITY_ISSUE/CONTAMINATION/DAMAGED/PENDING_QC/OTHER
    reason_text     TEXT,
    job_card_id     INT,
    so_id           INT,
    entity          TEXT CHECK (entity IN ('cfpl', 'cdpl'))
);

CREATE INDEX IF NOT EXISTS idx_reject_log_batch ON batch_rejection_log(batch_id);

-- Cascade events log (force reassign indent cascades)
CREATE TABLE IF NOT EXISTS cascade_events (
    event_id        SERIAL PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    old_so_id       INT,
    new_so_id       INT,
    old_indent_id   INT,
    new_indent_id   INT,
    executed_by     TEXT,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cascade_batch ON cascade_events(batch_id);

-- Add space constraint flag to internal issue notes
ALTER TABLE internal_issue_note ADD COLUMN IF NOT EXISTS is_space_constrained BOOLEAN DEFAULT FALSE;
ALTER TABLE internal_issue_note ADD COLUMN IF NOT EXISTS reject_reason TEXT;

-- Set NOT NULL defaults for warehouse_id/floor_id on inventory_batch
UPDATE inventory_batch SET warehouse_id = 'default' WHERE warehouse_id IS NULL;
UPDATE inventory_batch SET floor_id = 'rm_store' WHERE floor_id IS NULL;
ALTER TABLE inventory_batch ALTER COLUMN floor_id SET DEFAULT 'rm_store';

-- Reconciliation failures log
CREATE TABLE IF NOT EXISTS reconciliation_failures (
    failure_id      SERIAL PRIMARY KEY,
    sku_name        TEXT NOT NULL,
    entity          TEXT,
    expected_total  NUMERIC(15,3),
    actual_total    NUMERIC(15,3),
    discrepancy_kg  NUMERIC(15,3),
    status_breakdown JSONB,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,
    transaction_context TEXT
);

CREATE INDEX IF NOT EXISTS idx_recon_fail_entity ON reconciliation_failures(entity);

-- Legacy import log
CREATE TABLE IF NOT EXISTS legacy_import_log (
    import_id       SERIAL PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    item_code       TEXT NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    imported_by     TEXT,
    source_file_ref TEXT,
    entity          TEXT CHECK (entity IN ('cfpl', 'cdpl'))
);

CREATE INDEX IF NOT EXISTS idx_legacy_log_batch ON legacy_import_log(batch_id);

-- Add cancelled_at/cancelled_reason to purchase_indent
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS cancelled_reason TEXT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS cascade_event_id INT;

-- ═══════════════════════════════════════════════════════════════════
-- Migration: Material Accounting for Job Card Close
-- ═══════════════════════════════════════════════════════════════════

-- Per-BOM-line actual consumption tracking
CREATE TABLE IF NOT EXISTS job_card_material_consumption (
    consumption_id          SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    rm_indent_id            INT,
    material_sku_name       TEXT NOT NULL,
    item_type               TEXT NOT NULL DEFAULT 'rm',
    uom                     TEXT,
    bom_reqd_qty            NUMERIC(15,3) NOT NULL DEFAULT 0,
    issued_qty              NUMERIC(15,3) DEFAULT 0,
    actual_consumed_qty     NUMERIC(15,3),
    return_qty              NUMERIC(15,3) DEFAULT 0,
    variance_qty            NUMERIC(15,3),
    remarks                 TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_card_id, material_sku_name, item_type)
);
CREATE INDEX IF NOT EXISTS idx_mat_consumption_jc ON job_card_material_consumption(job_card_id);

-- By-products / off-grade categories
CREATE TABLE IF NOT EXISTS job_card_byproduct (
    byproduct_id            SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    category                TEXT NOT NULL,
    quantity_kg             NUMERIC(15,3) NOT NULL DEFAULT 0,
    remarks                 TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_card_id, category)
);
CREATE INDEX IF NOT EXISTS idx_byproduct_jc ON job_card_byproduct(job_card_id);

-- Material accounting summary (one row per job card)
CREATE TABLE IF NOT EXISTS job_card_material_accounting (
    accounting_id           SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id) UNIQUE,
    total_material_issued_kg NUMERIC(15,3) NOT NULL DEFAULT 0,
    fg_output_kg            NUMERIC(15,3) NOT NULL DEFAULT 0,
    process_loss_kg         NUMERIC(15,3) DEFAULT 0,
    process_loss_sub_categories JSONB,
    extra_give_away_kg      NUMERIC(15,3) DEFAULT 0,
    balance_material_kg     NUMERIC(15,3) DEFAULT 0,
    offgrade_total_kg       NUMERIC(15,3) DEFAULT 0,
    rejection_kg            NUMERIC(15,3) DEFAULT 0,
    wastage_kg              NUMERIC(15,3) DEFAULT 0,
    process_loss_pct        NUMERIC(8,3) DEFAULT 0,
    other_loss_pct          NUMERIC(8,3) DEFAULT 0,
    total_loss_pct          NUMERIC(8,3) DEFAULT 0,
    balance_difference_kg   NUMERIC(15,3) DEFAULT 0,
    is_balanced             BOOLEAN DEFAULT FALSE,
    balanced_by             TEXT,
    balanced_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mat_accounting_jc ON job_card_material_accounting(job_card_id);

-- Add extra columns to job_card_output
ALTER TABLE job_card_output ADD COLUMN IF NOT EXISTS extra_give_away_kg NUMERIC(15,3) DEFAULT 0;
ALTER TABLE job_card_output ADD COLUMN IF NOT EXISTS balance_material_kg NUMERIC(15,3) DEFAULT 0;
ALTER TABLE job_card_output ADD COLUMN IF NOT EXISTS wastage_kg NUMERIC(15,3) DEFAULT 0;

-- Add control_sample_kg to material accounting
ALTER TABLE job_card_material_accounting ADD COLUMN IF NOT EXISTS control_sample_kg NUMERIC(15,3) DEFAULT 0;

-- ═══════════════════════════════════════════════════════════════════
-- Migration: Store allocation tracking on purchase_indent
-- ═══════════════════════════════════════════════════════════════════
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS allocated_qty_kg NUMERIC(15,3);
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS allocated_by TEXT;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS allocated_at TIMESTAMPTZ;
ALTER TABLE purchase_indent ADD COLUMN IF NOT EXISTS insufficient_reason TEXT;
