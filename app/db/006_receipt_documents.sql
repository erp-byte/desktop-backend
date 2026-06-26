-- =========================================================================
-- 006_receipt_documents.sql
-- Schema for the documented /api/v1/receipt endpoints (COA + invoice/challan/LR).
--
-- Changes:
--   • coa_document — extended with the spec's columns; FK to po_line dropped
--     so a COA can anchor on qc_intimation_id / supplier_id / sku_id alone.
--     transaction_no/line_number become optional anchors; CHECK requires
--     at least one anchor of the spec set.
--   • receipt_document — NEW table for invoice/challan/lr/other uploads.
--   • Permission seeds: receipt.coa.{create,read,update,delete} and
--     receipt.invoice.create.
--
-- Idempotent. Safe to re-run. Requires PostgreSQL ≥ 11.
-- =========================================================================

-- ── coa_document: extend ─────────────────────────────────────────────────

-- Drop legacy FK to po_line so non-PO anchors are usable.
ALTER TABLE coa_document
    DROP CONSTRAINT IF EXISTS coa_document_transaction_no_line_number_fkey;

-- Make legacy NOT NULL anchors optional.
ALTER TABLE coa_document  ALTER COLUMN transaction_no DROP NOT NULL;
ALTER TABLE coa_document  ALTER COLUMN line_number    DROP NOT NULL;

-- Add the spec's columns.
ALTER TABLE coa_document
    ADD COLUMN IF NOT EXISTS dock_intimation_id  INT,
    ADD COLUMN IF NOT EXISTS qc_intimation_id    INT,
    ADD COLUMN IF NOT EXISTS sku_id              INT,
    ADD COLUMN IF NOT EXISTS sku_name_raw        TEXT,
    ADD COLUMN IF NOT EXISTS supplier_id         INT,
    ADD COLUMN IF NOT EXISTS lot_number          TEXT,
    ADD COLUMN IF NOT EXISTS s3_key              TEXT,
    ADD COLUMN IF NOT EXISTS file_name           TEXT,
    ADD COLUMN IF NOT EXISTS file_size_bytes     BIGINT,
    ADD COLUMN IF NOT EXISTS mime_type           TEXT,
    ADD COLUMN IF NOT EXISTS scan_status         TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS coa_status          TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS replaces_coa_id     TEXT,
    ADD COLUMN IF NOT EXISTS replaced_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS replaced_reason     TEXT,
    ADD COLUMN IF NOT EXISTS replaced_by         TEXT,
    ADD COLUMN IF NOT EXISTS deleted_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by          TEXT,
    ADD COLUMN IF NOT EXISTS delete_reason       TEXT,
    ADD COLUMN IF NOT EXISTS remarks             TEXT,
    ADD COLUMN IF NOT EXISTS entity              TEXT;

-- Allowlist for status enums.
ALTER TABLE coa_document DROP CONSTRAINT IF EXISTS coa_document_scan_status_check;
ALTER TABLE coa_document ADD  CONSTRAINT coa_document_scan_status_check
    CHECK (scan_status IN ('pending', 'clean', 'infected', 'skipped'));

ALTER TABLE coa_document DROP CONSTRAINT IF EXISTS coa_document_coa_status_check;
ALTER TABLE coa_document ADD  CONSTRAINT coa_document_coa_status_check
    CHECK (coa_status IN ('active', 'superseded', 'deleted'));

-- WR-05: keep coa_status='deleted' and deleted_at IS NOT NULL in sync.
-- Prevents the legacy bool/timestamp drift surface we hit on po_header.
-- Backfill any inconsistency before adding the constraint, in case a prior
-- migration set one without the other.
UPDATE coa_document SET deleted_at = NOW() WHERE coa_status = 'deleted' AND deleted_at IS NULL;
UPDATE coa_document SET coa_status = 'deleted' WHERE deleted_at IS NOT NULL AND coa_status <> 'deleted';

ALTER TABLE coa_document DROP CONSTRAINT IF EXISTS coa_document_deleted_consistency;
ALTER TABLE coa_document ADD  CONSTRAINT coa_document_deleted_consistency
    CHECK ((coa_status = 'deleted') = (deleted_at IS NOT NULL));

-- Anchor presence: at least one of the documented refs must be set.
ALTER TABLE coa_document DROP CONSTRAINT IF EXISTS coa_document_anchor_present;
ALTER TABLE coa_document ADD  CONSTRAINT coa_document_anchor_present
    CHECK (
        transaction_no IS NOT NULL
        OR dock_intimation_id IS NOT NULL
        OR qc_intimation_id IS NOT NULL
        OR sku_id IS NOT NULL
        OR sku_name_raw IS NOT NULL
    );

-- Self-FK for replacement chain (no ON DELETE; soft-deletes only).
ALTER TABLE coa_document DROP CONSTRAINT IF EXISTS coa_document_replaces_coa_id_fkey;
ALTER TABLE coa_document ADD  CONSTRAINT coa_document_replaces_coa_id_fkey
    FOREIGN KEY (replaces_coa_id) REFERENCES coa_document(coa_id);

CREATE INDEX IF NOT EXISTS idx_coa_dock_intim   ON coa_document(dock_intimation_id);
CREATE INDEX IF NOT EXISTS idx_coa_qc_intim     ON coa_document(qc_intimation_id);
CREATE INDEX IF NOT EXISTS idx_coa_supplier     ON coa_document(supplier_id);
CREATE INDEX IF NOT EXISTS idx_coa_lot          ON coa_document(lot_number);
CREATE INDEX IF NOT EXISTS idx_coa_status       ON coa_document(coa_status);
CREATE INDEX IF NOT EXISTS idx_coa_uploaded_at  ON coa_document(uploaded_at DESC);

-- ── receipt_document: NEW table for invoice/challan/lr/other ─────────────

CREATE TABLE IF NOT EXISTS receipt_document (
    document_id          BIGSERIAL    PRIMARY KEY,
    document_type        TEXT         NOT NULL DEFAULT 'invoice',
    transaction_no       TEXT,
    po_number            TEXT,
    dock_intimation_id   INT,
    supplier_id          INT,
    entity               TEXT,
    s3_key               TEXT         NOT NULL,
    file_name            TEXT         NOT NULL,
    file_size_bytes      BIGINT       NOT NULL,
    mime_type            TEXT         NOT NULL,
    scan_status          TEXT         NOT NULL DEFAULT 'pending',
    remarks              TEXT,
    uploaded_by          TEXT,
    uploaded_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deleted_at           TIMESTAMPTZ,
    deleted_by           TEXT,
    delete_reason        TEXT
);

ALTER TABLE receipt_document DROP CONSTRAINT IF EXISTS receipt_document_type_check;
ALTER TABLE receipt_document ADD  CONSTRAINT receipt_document_type_check
    CHECK (document_type IN ('invoice', 'challan', 'lr', 'other'));

ALTER TABLE receipt_document DROP CONSTRAINT IF EXISTS receipt_document_scan_status_check;
ALTER TABLE receipt_document ADD  CONSTRAINT receipt_document_scan_status_check
    CHECK (scan_status IN ('pending', 'clean', 'infected', 'skipped'));

CREATE INDEX IF NOT EXISTS idx_recv_doc_txn         ON receipt_document(transaction_no);
CREATE INDEX IF NOT EXISTS idx_recv_doc_dock_intim  ON receipt_document(dock_intimation_id);
CREATE INDEX IF NOT EXISTS idx_recv_doc_supplier    ON receipt_document(supplier_id);
CREATE INDEX IF NOT EXISTS idx_recv_doc_uploaded_at ON receipt_document(uploaded_at DESC);

-- ── Permission seeds ─────────────────────────────────────────────────────

INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES
    ('receipt', 'coa',     NULL, 'read',   'View COA documents'),
    ('receipt', 'coa',     NULL, 'create', 'Upload new COA documents'),
    ('receipt', 'coa',     NULL, 'update', 'Replace (supersede) COA documents'),
    ('receipt', 'coa',     NULL, 'delete', 'Soft-delete COA documents'),
    ('receipt', 'invoice', NULL, 'create', 'Upload invoice/challan/LR/other receipt-side documents'),
    ('receipt', 'invoice', NULL, 'read',   'View receipt-side documents')
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

-- Grant admin all four COA + both invoice perms (admin grants are also
-- handled by the catch-all in auth_schema.sql; explicit for clarity).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'admin'
   AND p.module = 'receipt'
ON CONFLICT DO NOTHING;

-- store_head: read + create + update + delete (all four COA, plus invoice)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'receipt'
ON CONFLICT DO NOTHING;

-- inventory_manager: read + create + update (no delete) for COA; invoice both
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'inventory_manager'
   AND p.module = 'receipt'
   AND ((p.sub_module = 'coa'     AND p.action IN ('read', 'create', 'update'))
     OR (p.sub_module = 'invoice'))
ON CONFLICT DO NOTHING;

-- viewer: read-only on both
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'viewer'
   AND p.module = 'receipt' AND p.action = 'read'
ON CONFLICT DO NOTHING;
