-- =========================================================================
-- SAP MM Alignment — Movement Types, Material Documents, QC Hold, FEFO
-- =========================================================================

-- 1. Material Document (SAP's MIGO equivalent — single source of truth)
CREATE TABLE IF NOT EXISTS material_document (
    id                  SERIAL PRIMARY KEY,
    mat_doc_id          TEXT NOT NULL UNIQUE,            -- MATDOC-YYYYMMDD-SEQ
    doc_date            DATE NOT NULL DEFAULT CURRENT_DATE,
    posting_date        DATE NOT NULL DEFAULT CURRENT_DATE,
    movement_type       TEXT NOT NULL,                   -- SAP-aligned: 101, 261, 262, 301, etc.
    reference_type      TEXT,                            -- PO, JOB_CARD, TRANSFER, QC, RTV, ISN
    reference_id        TEXT,                            -- PO number, JC ID, ISN ID, etc.
    created_by          TEXT NOT NULL,
    entity              TEXT DEFAULT 'cfpl' CHECK (entity IN ('cfpl','cdpl')),
    reversal_of         TEXT,                            -- mat_doc_id of reversed document
    is_reversal         BOOLEAN DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_matdoc_date ON material_document(posting_date);
CREATE INDEX IF NOT EXISTS idx_matdoc_mvt ON material_document(movement_type);
CREATE INDEX IF NOT EXISTS idx_matdoc_ref ON material_document(reference_type, reference_id);

CREATE TABLE IF NOT EXISTS material_document_line (
    id                  SERIAL PRIMARY KEY,
    mat_doc_id          TEXT NOT NULL REFERENCES material_document(mat_doc_id),
    line_number         INT NOT NULL DEFAULT 1,
    sku_name            TEXT NOT NULL,
    batch_id            TEXT,
    movement_type       TEXT NOT NULL,
    quantity_kg         NUMERIC(15,3) NOT NULL,
    uom                 TEXT DEFAULT 'kg',
    from_location       TEXT,                            -- warehouse/godown/floor
    to_location         TEXT,
    from_status         TEXT,
    to_status           TEXT,
    lot_number          TEXT,
    box_id              TEXT,
    UNIQUE (mat_doc_id, line_number)
);
CREATE INDEX IF NOT EXISTS idx_matdocl_doc ON material_document_line(mat_doc_id);
CREATE INDEX IF NOT EXISTS idx_matdocl_batch ON material_document_line(batch_id);

CREATE SEQUENCE IF NOT EXISTS seq_matdoc START 1;

-- 2. Movement Type Reference (lookup table)
CREATE TABLE IF NOT EXISTS movement_type_ref (
    movement_type       TEXT PRIMARY KEY,
    description         TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('IN','OUT','TRANSFER','REVERSAL')),
    affects_stock       BOOLEAN DEFAULT TRUE,
    reversal_type       TEXT                             -- the movement type that reverses this one
);

INSERT INTO movement_type_ref (movement_type, description, direction, affects_stock, reversal_type) VALUES
    ('101', 'Goods Receipt against PO',           'IN',       TRUE, '102'),
    ('102', 'GR Reversal',                        'REVERSAL', TRUE, NULL),
    ('122', 'Return to Vendor',                   'OUT',      TRUE, NULL),
    ('201', 'Goods Issue to Cost Center',          'OUT',      TRUE, '202'),
    ('202', 'GI Reversal (Cost Center)',           'REVERSAL', TRUE, NULL),
    ('261', 'Goods Issue to Production Order',     'OUT',      TRUE, '262'),
    ('262', 'Return from Production Order',        'REVERSAL', TRUE, NULL),
    ('301', 'Transfer — Plant to Plant (1 step)',  'TRANSFER', TRUE, '302'),
    ('302', 'Transfer Reversal',                   'REVERSAL', TRUE, NULL),
    ('311', 'Transfer — Storage Location',         'TRANSFER', TRUE, '312'),
    ('312', 'SL Transfer Reversal',                'REVERSAL', TRUE, NULL),
    ('321', 'QC Hold to Unrestricted (Accept)',    'TRANSFER', TRUE, NULL),
    ('322', 'QC Hold to Blocked (Reject)',         'TRANSFER', TRUE, NULL),
    ('531', 'FG Receipt from Production',          'IN',       TRUE, '532'),
    ('532', 'FG Receipt Reversal',                 'REVERSAL', TRUE, NULL),
    ('551', 'Scrapping / Write-off',               'OUT',      TRUE, NULL),
    ('561', 'Initial Stock Upload / Legacy',       'IN',       TRUE, NULL)
ON CONFLICT (movement_type) DO NOTHING;

-- 3. Add movement_type column to existing tables
ALTER TABLE issue_note ADD COLUMN IF NOT EXISTS movement_type TEXT DEFAULT '261';
ALTER TABLE issue_note_line ADD COLUMN IF NOT EXISTS movement_type TEXT DEFAULT '261';

-- 4. Add QC_HOLD to inventory batch status (if inventory_batch has a status CHECK)
-- This is a safe ALTER — adds QC_HOLD to the valid statuses
-- Note: inventory_batch.status is TEXT without CHECK in existing schema, so this just documents it
-- The VALID_TRANSITIONS in inventory_service.py needs updating (done in Python)

-- 5. Add batch strategy + shelf life fields to SKU master
ALTER TABLE all_sku ADD COLUMN IF NOT EXISTS batch_strategy TEXT DEFAULT 'FIFO'
    CHECK (batch_strategy IN ('FIFO', 'FEFO'));
ALTER TABLE all_sku ADD COLUMN IF NOT EXISTS min_shelf_life_days INT DEFAULT 0;

-- 6. Add backflush flag to BOM line
ALTER TABLE bom_line ADD COLUMN IF NOT EXISTS staging_method TEXT DEFAULT 'pick'
    CHECK (staging_method IN ('pick', 'backflush', 'floor_stock'));

-- 7. Add staging_area to floor locations (extend allowed transitions)
-- This is handled in Python (floor_tracker.py) — just documenting here

-- 8. GR tolerance on PO line
ALTER TABLE po_line ADD COLUMN IF NOT EXISTS gr_tolerance_pct NUMERIC(5,2) DEFAULT 5.0;
ALTER TABLE po_line ADD COLUMN IF NOT EXISTS received_qty_kg NUMERIC(15,3) DEFAULT 0;

-- 9. Inter-entity stock transfer
CREATE TABLE IF NOT EXISTS inter_entity_transfer (
    id                  SERIAL PRIMARY KEY,
    transfer_id         TEXT NOT NULL UNIQUE,            -- IET-YYYYMMDD-SEQ
    from_entity         TEXT NOT NULL CHECK (from_entity IN ('cfpl','cdpl')),
    to_entity           TEXT NOT NULL CHECK (to_entity IN ('cfpl','cdpl')),
    transfer_date       DATE NOT NULL DEFAULT CURRENT_DATE,
    status              TEXT NOT NULL DEFAULT 'dispatched'
                        CHECK (status IN ('dispatched','in_transit','received','cancelled')),
    dispatched_by       TEXT,
    dispatched_at       TIMESTAMPTZ,
    received_by         TEXT,
    received_at         TIMESTAMPTZ,
    mat_doc_dispatch    TEXT,                            -- material doc for dispatch
    mat_doc_receipt     TEXT,                            -- material doc for receipt
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inter_entity_transfer_line (
    id                  SERIAL PRIMARY KEY,
    transfer_id         TEXT NOT NULL REFERENCES inter_entity_transfer(transfer_id),
    batch_id            TEXT NOT NULL,
    sku_name            TEXT NOT NULL,
    quantity_kg         NUMERIC(15,3) NOT NULL,
    lot_number          TEXT,
    dispatched_qty_kg   NUMERIC(15,3),
    received_qty_kg     NUMERIC(15,3)
);
CREATE INDEX IF NOT EXISTS idx_ietl_transfer ON inter_entity_transfer_line(transfer_id);

CREATE SEQUENCE IF NOT EXISTS seq_iet START 1;

-- 10. Purchase indent approval tiers
ALTER TABLE production_indent ADD COLUMN IF NOT EXISTS indent_value NUMERIC(15,2);
ALTER TABLE production_indent ADD COLUMN IF NOT EXISTS approval_level TEXT DEFAULT 'standard'
    CHECK (approval_level IN ('auto', 'standard', 'management'));
