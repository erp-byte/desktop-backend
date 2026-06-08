-- =========================================================================
-- 037_ncr.sql
--
-- NCR (Non-Conformance Report) → ONE table (minimum-tables design).
-- Mirrors data/candor_rm_ncr_fields.xlsx ("NCR Report Fields" sheet).
--
-- The two 1:N child sets the form implies — failed parameters and supplier
-- CAPA actions — are folded into JSONB array columns instead of separate
-- ncr_parameter_detail / ncr_supplier_action tables, per the "keep tables
-- minimum" decision.
--
--   failed_parameters : [ {
--       param_code, reading_id, spec_value, actual_value,
--       deviation_type ('above_max'|'below_min'|'presence'|'absence'),
--       severity ('critical'|'major'|'minor'),
--       quantity_impacted_kg, disposition
--   }, ... ]   -- param_code → qc_parameter.code; reading_id → qc_inward_reading
--
--   supplier_actions  : [ {
--       action_type ('root_cause'|'correction'|'preventive'),
--       description, responsible_party, target_date, actual_closure_date,
--       evidence_file_url, verified_by, verification_date, is_effective
--   }, ... ]
--
-- PK is an app-supplied 8-digit time-based BIGINT (app.core.helpers
-- .new_short_time_id), consistent with the rest of the QC surface. ncr_no is
-- the formatted human reference (e.g. NCR/2026/0001).
--
-- Fully idempotent — safe to re-run. PostgreSQL >= 13.
-- =========================================================================

CREATE TABLE IF NOT EXISTS ncr_record (
    ncr_id                  BIGINT PRIMARY KEY,            -- app-supplied 8-digit time id
    ncr_no                  TEXT UNIQUE,                   -- formatted, e.g. NCR/2026/0001

    -- Header
    supplier_id             INT,
    supplier_name           TEXT,                          -- denormalized for display
    other_party             TEXT,                          -- transporter / 3rd party
    detected_by             INT,

    -- Lot info
    invoice_challan_ref     TEXT,
    batch_no                TEXT,
    transaction_no          TEXT,                          -- + line_number → derives product desc
    line_number             INT,
    product_description     TEXT,                          -- denormalized (po_line.sku_name) for display
    rc_no                   TEXT,
    quantity                NUMERIC,                       -- affected qty (kg)

    -- Reason
    reason_nonconformity    TEXT,
    food_safety_flag        BOOLEAN NOT NULL DEFAULT FALSE,
    documented_by           INT,
    documented_date         DATE,

    -- Disposition
    disposition             TEXT,                          -- rejected | returned | accepted_dispensation
    financial_action        TEXT,                          -- correction | credit | debit
    supplier_action_type    TEXT,                          -- info_only | investigation_required
    target_completion_date  DATE,

    -- Sign-off
    authorized_person       INT,
    authorized_date         DATE,
    received_by             INT,
    received_date           DATE,
    created_by              INT,
    approved_by             INT,

    -- Computed / rollup (app-populated)
    ncr_category            TEXT,
    severity_rollup         TEXT,                          -- critical | major | minor
    financial_impact_inr    NUMERIC,
    closure_tat_days        INT,
    status                  TEXT NOT NULL DEFAULT 'open',  -- open | in_supplier_action | closed

    -- Folded 1:N details
    failed_parameters       JSONB NOT NULL DEFAULT '[]'::jsonb,
    supplier_actions        JSONB NOT NULL DEFAULT '[]'::jsonb,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_ncr_disposition CHECK (
        disposition IS NULL OR disposition IN ('rejected','returned','accepted_dispensation')),
    CONSTRAINT ck_ncr_financial CHECK (
        financial_action IS NULL OR financial_action IN ('correction','credit','debit')),
    CONSTRAINT ck_ncr_supplier_action CHECK (
        supplier_action_type IS NULL OR supplier_action_type IN ('info_only','investigation_required')),
    CONSTRAINT ck_ncr_severity CHECK (
        severity_rollup IS NULL OR severity_rollup IN ('critical','major','minor')),
    CONSTRAINT ck_ncr_status CHECK (
        status IN ('open','in_supplier_action','closed'))
);

CREATE INDEX IF NOT EXISTS idx_ncr_status      ON ncr_record(status);
CREATE INDEX IF NOT EXISTS idx_ncr_supplier    ON ncr_record(supplier_id);
CREATE INDEX IF NOT EXISTS idx_ncr_transaction ON ncr_record(transaction_no);
