-- =========================================================================
-- IMS New Module Tables (v1)
-- Production Indent, Issue Notes, Lot Blocking, QC, RTV Disposition,
-- Off-Grade, Write-Off, Amendments
-- =========================================================================

-- 1. Production Indent (FG/SFG) — Section A2
CREATE TABLE IF NOT EXISTS production_indent (
    id                      SERIAL PRIMARY KEY,
    prod_indent_id          TEXT NOT NULL UNIQUE,            -- PRDI-YYYYMMDD-SEQ
    item_description        TEXT NOT NULL,
    item_category           TEXT,
    sub_category            TEXT,
    material_type           TEXT NOT NULL CHECK (material_type IN ('FG', 'SFG')),
    uom                     TEXT DEFAULT 'kg',
    required_qty            NUMERIC(12,2) NOT NULL,
    available_qty           NUMERIC(12,2) DEFAULT 0,
    shortfall_qty           NUMERIC(12,2) DEFAULT 0,
    triggered_by_job_card   TEXT,
    triggered_by_so         TEXT,
    customer_name           TEXT,
    maker_user              TEXT NOT NULL,
    checker_user            TEXT,
    checker_comment         TEXT,
    status                  TEXT NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','submitted','approved',
                                   'internal_jc_created','fulfilled','cancelled')),
    linked_internal_order   TEXT,
    linked_internal_jc      TEXT,
    entity                  TEXT DEFAULT 'cfpl' CHECK (entity IN ('cfpl','cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at             TIMESTAMPTZ,
    fulfilled_at            TIMESTAMPTZ,
    cancel_reason           TEXT
);
CREATE INDEX IF NOT EXISTS idx_prdi_status      ON production_indent(status);
CREATE INDEX IF NOT EXISTS idx_prdi_entity      ON production_indent(entity);
CREATE INDEX IF NOT EXISTS idx_prdi_item_so     ON production_indent(item_description, triggered_by_so);
CREATE INDEX IF NOT EXISTS idx_prdi_created     ON production_indent(created_at DESC);

-- 2. Internal Order — Section E3
CREATE TABLE IF NOT EXISTS internal_order (
    id                      SERIAL PRIMARY KEY,
    internal_order_id       TEXT NOT NULL UNIQUE,            -- INT-ORD-YYYYMMDD-SEQ
    prod_indent_id          TEXT REFERENCES production_indent(prod_indent_id),
    item_description        TEXT NOT NULL,
    material_type           TEXT NOT NULL,
    required_qty            NUMERIC(12,2),
    status                  TEXT NOT NULL DEFAULT 'created'
                            CHECK (status IN ('created','jc_assigned','in_progress','completed','cancelled')),
    entity                  TEXT DEFAULT 'cfpl' CHECK (entity IN ('cfpl','cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_intord_status ON internal_order(status);

-- 3. Internal Job Card — Section E3
CREATE TABLE IF NOT EXISTS internal_job_card (
    id                      SERIAL PRIMARY KEY,
    internal_jc_id          TEXT NOT NULL UNIQUE,
    internal_order_id       TEXT NOT NULL REFERENCES internal_order(internal_order_id),
    parent_job_card_id      TEXT,
    parent_so_ref           TEXT,
    fg_sku_name             TEXT,
    status                  TEXT NOT NULL DEFAULT 'created'
                            CHECK (status IN ('created','assigned','in_progress','completed','cancelled')),
    bom_data                JSONB,
    entity                  TEXT DEFAULT 'cfpl' CHECK (entity IN ('cfpl','cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_intjc_order ON internal_job_card(internal_order_id);

-- 4. Issue Note — Section D4
CREATE TABLE IF NOT EXISTS issue_note (
    id                      SERIAL PRIMARY KEY,
    issue_note_id           TEXT NOT NULL UNIQUE,            -- ISN-YYYYMMDD-SEQ
    job_card_id             TEXT NOT NULL,
    so_id                   TEXT,
    customer_name           TEXT,
    bom_line_id             TEXT,
    issued_by               TEXT NOT NULL,
    issued_at               TIMESTAMPTZ DEFAULT NOW(),
    status                  TEXT NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','confirmed','partially_reversed','reversed')),
    reservation_expires_at  TIMESTAMPTZ,
    total_weight_kg         NUMERIC(12,3) DEFAULT 0,
    entity                  TEXT DEFAULT 'cfpl' CHECK (entity IN ('cfpl','cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_isn_jc     ON issue_note(job_card_id);
CREATE INDEX IF NOT EXISTS idx_isn_status ON issue_note(status);

-- 5. Issue Note Line — Section D4
CREATE TABLE IF NOT EXISTS issue_note_line (
    id                      SERIAL PRIMARY KEY,
    issue_note_id           TEXT NOT NULL REFERENCES issue_note(issue_note_id),
    bom_line_id             TEXT,
    sku                     TEXT,
    material_type           TEXT,
    lot_number              TEXT,
    lot_id                  TEXT,
    tr_number               TEXT,
    warehouse               TEXT,
    net_wt_issued           NUMERIC(12,3) NOT NULL,
    qty_cartons             INT,
    box_id                  TEXT,
    fifo_skipped            BOOLEAN DEFAULT FALSE,
    skip_reason             TEXT
);
CREATE INDEX IF NOT EXISTS idx_isnl_note ON issue_note_line(issue_note_id);

-- 6. Lot Block — Section C2/D3
CREATE TABLE IF NOT EXISTS lot_block (
    id                      SERIAL PRIMARY KEY,
    block_id                TEXT NOT NULL UNIQUE,
    transaction_no          TEXT,
    lot_number              TEXT NOT NULL,
    batch_id                TEXT,
    blocked_for_so          TEXT,
    blocked_for_customer    TEXT,
    blocked_by_user         TEXT NOT NULL,
    blocked_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    skip_reason             TEXT,
    comment                 TEXT,
    previous_so             TEXT,
    force_assigned_by       TEXT,
    force_assigned_at       TIMESTAMPTZ,
    override_comment        TEXT,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_lb_lot    ON lot_block(lot_number, is_active);
CREATE INDEX IF NOT EXISTS idx_lb_batch  ON lot_block(batch_id, is_active);
CREATE INDEX IF NOT EXISTS idx_lb_so     ON lot_block(blocked_for_so) WHERE is_active = TRUE;

-- 7. FIFO Skip Log — Section D3
CREATE TABLE IF NOT EXISTS fifo_skip_log (
    id                      SERIAL PRIMARY KEY,
    batch_id                TEXT NOT NULL,
    job_card_id             TEXT,
    reason                  TEXT NOT NULL,
    detail                  TEXT,
    disposition             TEXT CHECK (disposition IN ('leave_available','block_for_so','hold','quarantine','reject')),
    block_for_so            TEXT,
    skipped_by              TEXT NOT NULL,
    skipped_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fsl_batch ON fifo_skip_log(batch_id);

-- 8. QC Inspection — Section G1
CREATE TABLE IF NOT EXISTS qc_inspection (
    id                      SERIAL PRIMARY KEY,
    inspection_id           TEXT NOT NULL UNIQUE,
    job_card_id             INT NOT NULL,
    jc_number               TEXT,
    fg_sku_name             TEXT,
    customer_name           TEXT,
    floor                   TEXT,
    process_step            TEXT,
    checkpoint_type         TEXT NOT NULL
                            CHECK (checkpoint_type IN ('pre_production','in_process',
                                   'post_production','rtv_disposition')),
    inspector_user          TEXT,
    inspection_date         TIMESTAMPTZ,
    result                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (result IN ('pending','pass','fail','conditional_pass')),
    findings                TEXT,
    corrective_action       TEXT,
    signed_off_at           TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qci_jc         ON qc_inspection(job_card_id);
CREATE INDEX IF NOT EXISTS idx_qci_result     ON qc_inspection(result);
CREATE INDEX IF NOT EXISTS idx_qci_checkpoint ON qc_inspection(checkpoint_type);

-- 9. RTV Disposition — Section H1
CREATE TABLE IF NOT EXISTS rtv_disposition (
    id                      SERIAL PRIMARY KEY,
    disposition_id          TEXT NOT NULL UNIQUE,
    rtv_id                  TEXT NOT NULL,
    item_description        TEXT,
    qty                     NUMERIC(12,2),
    net_weight              NUMERIC(12,3),
    source_type             TEXT DEFAULT 'RTV',
    disposition_type        TEXT NOT NULL DEFAULT 'pending'
                            CHECK (disposition_type IN ('pending','reprocess','offgrade',
                                   'discard','return_to_vendor')),
    decided_by              TEXT,
    decided_at              TIMESTAMPTZ,
    qc_remarks              TEXT,
    linked_internal_order   TEXT,
    linked_offgrade_lot     TEXT,
    discard_approved        BOOLEAN DEFAULT FALSE,
    entity                  TEXT DEFAULT 'cfpl' CHECK (entity IN ('cfpl','cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rtvd_rtv  ON rtv_disposition(rtv_id);
CREATE INDEX IF NOT EXISTS idx_rtvd_type ON rtv_disposition(disposition_type);

-- 10. Off-Grade Inventory — Section H3
CREATE TABLE IF NOT EXISTS off_grade_inventory (
    id                      SERIAL PRIMARY KEY,
    offgrade_id             TEXT NOT NULL UNIQUE,
    original_tr_number      TEXT,
    original_lot_number     TEXT,
    item_description        TEXT NOT NULL,
    material_type           TEXT,
    qty                     NUMERIC(12,2),
    net_weight              NUMERIC(12,3),
    source_type             TEXT CHECK (source_type IN ('RTV','JC_Closure_Rejection','QC_Rejection')),
    source_id               TEXT,
    condition_notes         TEXT,
    disposition             TEXT NOT NULL DEFAULT 'Pending Decision'
                            CHECK (disposition IN ('Sell','Discard','Pending Decision')),
    management_decision_by  TEXT,
    management_decision_at  TIMESTAMPTZ,
    entity                  TEXT DEFAULT 'cfpl' CHECK (entity IN ('cfpl','cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ogi_entity ON off_grade_inventory(entity);
CREATE INDEX IF NOT EXISTS idx_ogi_disp   ON off_grade_inventory(disposition);

-- 11. Write-Off Ledger — Section H4
CREATE TABLE IF NOT EXISTS write_off_ledger (
    id                      SERIAL PRIMARY KEY,
    rtv_id                  TEXT,
    offgrade_id             TEXT,
    item_description        TEXT NOT NULL,
    lot_number              TEXT,
    qty                     NUMERIC(12,2),
    net_weight              NUMERIC(12,3),
    reason                  TEXT NOT NULL,
    authorised_by           TEXT NOT NULL,
    written_off_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 12. Amendment Log — Section I2
CREATE TABLE IF NOT EXISTS amendment_log (
    id                      SERIAL PRIMARY KEY,
    record_id               TEXT NOT NULL,
    record_type             TEXT NOT NULL,
    field_name              TEXT NOT NULL,
    previous_value          TEXT,
    new_value               TEXT,
    changed_by              TEXT NOT NULL,
    changed_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_aml_record ON amendment_log(record_id, record_type);
CREATE INDEX IF NOT EXISTS idx_aml_field  ON amendment_log(record_id, record_type, field_name);

-- =========================================================================
-- SEQUENCES for human-readable IDs
-- =========================================================================
CREATE SEQUENCE IF NOT EXISTS seq_prdi START 1;
CREATE SEQUENCE IF NOT EXISTS seq_int_ord START 1;
CREATE SEQUENCE IF NOT EXISTS seq_int_jc START 1;
CREATE SEQUENCE IF NOT EXISTS seq_isn START 1;
CREATE SEQUENCE IF NOT EXISTS seq_block START 1;
CREATE SEQUENCE IF NOT EXISTS seq_qci START 1;
CREATE SEQUENCE IF NOT EXISTS seq_rtvd START 1;
CREATE SEQUENCE IF NOT EXISTS seq_ogi START 1;
