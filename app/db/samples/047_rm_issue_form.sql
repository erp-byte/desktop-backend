-- 047_rm_issue_form.sql  (NPD plan §10 — RM Issue / Collection Form, Document 015)
-- Digitises the live "Raw Material Issue / Collection Form – NPD Trials" form: a
-- maker-checker requisition (Requested By = NPD author, Issued By = Store) that
-- BACKS Step A of the inventory accounting backbone. Raising an indent snapshots
-- the recipe's RM lines (reqd_qty); when the Store issues, they record issued_qty
-- + lot_no per line and that fires the 265 Goods Issue (OWN lines only — customer
-- / off-master lines are recorded for traceability, no posting).
--
-- Own state machine: DRAFT -> SUBMITTED -> APPROVED -> ISSUED -> CLOSED (+ CANCELLED).
-- House numbering: RMIF-YYYYMMDD-NNNN via seq_rm_issue_form.

CREATE TABLE IF NOT EXISTS rm_issue_form (
    id                  SERIAL PRIMARY KEY,
    form_number         TEXT UNIQUE NOT NULL,             -- RMIF-YYYYMMDD-NNNN
    trial_name          TEXT,
    product_name        TEXT,
    customer_name       TEXT,                              -- NULL = Internal Use
    purpose_tag         TEXT,                              -- LAB_TRIAL, TASTING_SENSORY, ...
    source_type         TEXT,                              -- 'NPD_DEV_JC' | 'SAMPLE_REQ' | 'STANDALONE'
    source_id           INT,
    requisition_id      INT REFERENCES sample_requisitions(id),
    entity              TEXT NOT NULL DEFAULT 'cfpl' CHECK (entity IN ('cfpl', 'cdpl')),
    status              TEXT NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN ('DRAFT', 'SUBMITTED', 'APPROVED', 'ISSUED', 'CLOSED', 'CANCELLED')),
    requested_by        INT REFERENCES auth_user(user_id),  -- maker (NPD author)
    requested_at        TIMESTAMPTZ,
    issued_by           INT REFERENCES auth_user(user_id),  -- checker (Store)
    issued_at           TIMESTAMPTZ,
    issue_mat_doc_id    TEXT,                               -- the 265 material_document posted at issue
    cancellation_reason TEXT,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rm_issue_form_status ON rm_issue_form(status);
CREATE INDEX IF NOT EXISTS idx_rm_issue_form_source ON rm_issue_form(source_type, source_id);
CREATE SEQUENCE IF NOT EXISTS seq_rm_issue_form START 1;

CREATE TABLE IF NOT EXISTS rm_issue_form_lines (
    id              SERIAL PRIMARY KEY,
    form_id         INT NOT NULL REFERENCES rm_issue_form(id) ON DELETE CASCADE,
    sku_id          INT REFERENCES all_sku(sku_id),         -- nullable (off-master / customer)
    sku_name        TEXT NOT NULL,
    location        TEXT,                                   -- bin / owner, e.g. "Barline – Abhishek Rane"
    lot_no          TEXT,                                   -- FEFO/FIFO batch, filled at issue
    reqd_qty        NUMERIC(15, 3) NOT NULL DEFAULT 0,       -- snapshot from the recipe
    issued_qty      NUMERIC(15, 3),                          -- filled by Store at issue time
    uom             TEXT NOT NULL DEFAULT 'kg',
    ownership       TEXT NOT NULL DEFAULT 'OWN' CHECK (ownership IN ('OWN', 'CUSTOMER')),
    is_off_master   BOOLEAN NOT NULL DEFAULT FALSE,
    unit_cost       NUMERIC(15, 4),                          -- nullable, pre-wired for costing
    notes           TEXT,
    line_order      INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rm_issue_form_lines ON rm_issue_form_lines(form_id);
