-- ===========================================================================
-- 000_external_stub_tables.sql   (TEST DB — applied FIRST, before 006 / 030)
-- ---------------------------------------------------------------------------
-- The transfer + vendor + receipt(coa) tables shown in Candor_System_Wiring.html
-- are owned by a SEPARATE backend (transfer_backend_reference) and created there
-- via runtime DDL — there is NO `CREATE TABLE` for them anywhere in server_replica.
-- Yet server_replica migrations REFERENCE them and would FAIL on a fresh DB:
--     006_receipt_documents.sql : ALTERs coa_document + FK -> coa_document(coa_id)
--     030_vendor_history.sql    : FK -> vendor_master(vendor_id)   (this one is
--                                  even in the canonical runner, so the runner
--                                  itself assumes vendor_master already exists)
--
-- These STUBS create the 14 wiring tables so (a) the build succeeds and (b) the
-- test schema is complete ("all tables in the HTML"). Columns come from the
-- wiring's field lists; the two PK types are matched to the server_replica FK
-- references (coa_document.coa_id = TEXT; vendor_master.vendor_id = VARCHAR(32)).
-- Everything else is best-effort — the canonical DDL lives in the transfer/vendor
-- backend. No inter-stub FKs (kept order-independent). Idempotent.
-- ===========================================================================

-- ── receipt ────────────────────────────────────────────────────────────────
-- coa_id TEXT to match 006's `replaces_coa_id TEXT REFERENCES coa_document(coa_id)`;
-- transaction_no/line_number present so 006's `ALTER COLUMN ... DROP NOT NULL` works.
-- 006 then ADDs the remaining columns (dock_intimation_id, scan_status, ...).
CREATE TABLE IF NOT EXISTS coa_document (
    coa_id          TEXT PRIMARY KEY,
    transaction_no  TEXT,
    line_number     INT,
    uploaded_at     TIMESTAMPTZ            -- 006 builds idx_coa_uploaded_at on it
);

-- ── vendor module ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vendor_master (
    vendor_id      VARCHAR(32) PRIMARY KEY,   -- matches 030's REFERENCES vendor_master(vendor_id)
    supplier_code  TEXT,
    name           TEXT,
    status         TEXT,
    created_by     TEXT
);
CREATE TABLE IF NOT EXISTS vendor_banking (
    bank_id     BIGINT PRIMARY KEY,
    vendor_id   VARCHAR(32),
    account_no  TEXT,
    is_primary  BOOLEAN
);
CREATE TABLE IF NOT EXISTS vendor_contract (
    contract_id    BIGINT PRIMARY KEY,
    vendor_id      VARCHAR(32),
    contract_type  TEXT
);
CREATE TABLE IF NOT EXISTS vendor_document (
    doc_id     BIGINT PRIMARY KEY,
    vendor_id  VARCHAR(32),
    doc_type   TEXT,
    valid_to   DATE
);

-- ── transfer / inter-unit (IMS) module ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS warehouse_sites (
    site_code  TEXT PRIMARY KEY,
    site_name  TEXT,
    active     BOOLEAN
);
CREATE TABLE IF NOT EXISTS transfer_requests (
    id           BIGINT PRIMARY KEY,
    request_no   TEXT,
    from_site    TEXT,
    to_site      TEXT,
    status       TEXT,
    reason_code  TEXT
);
CREATE TABLE IF NOT EXISTS transfer_request_lines (
    id             BIGINT PRIMARY KEY,
    request_id     BIGINT,
    item_category  TEXT,
    qty            NUMERIC(15,3),
    net_weight     NUMERIC(15,3)
);
CREATE TABLE IF NOT EXISTS interunit_transfers_header (
    id          BIGINT PRIMARY KEY,
    challan_no  TEXT,
    from_site   TEXT,
    to_site     TEXT,
    request_id  BIGINT,
    status      TEXT
);
CREATE TABLE IF NOT EXISTS interunit_transfers_lines (
    id             BIGINT PRIMARY KEY,
    header_id      BIGINT,
    item_category  TEXT,
    qty            NUMERIC(15,3)
);
CREATE TABLE IF NOT EXISTS interunit_transfer_boxes (
    id          BIGINT PRIMARY KEY,
    header_id   BIGINT,
    box_id      TEXT,
    lot_number  TEXT,
    net_weight  NUMERIC(15,3)
);
CREATE TABLE IF NOT EXISTS interunit_transfer_in_header (
    id                   BIGINT PRIMARY KEY,
    transfer_out_id      BIGINT,
    grn_number           TEXT,
    receiving_warehouse  TEXT,
    status               TEXT
);
CREATE TABLE IF NOT EXISTS interunit_transfer_in_boxes (
    id          BIGINT PRIMARY KEY,
    header_id   BIGINT,
    box_id      TEXT,
    is_matched  BOOLEAN,
    lot_number  TEXT
);
CREATE TABLE IF NOT EXISTS pending_transfer_stock (
    id                 BIGINT PRIMARY KEY,
    transfer_out_id    BIGINT,
    box_id             TEXT,
    status             TEXT,
    destination_table  TEXT
);

-- ── out-of-band COLUMN patches on FOUNDATIONAL tables ───────────────────────
-- Columns server_replica migrations read but never create (added manually in prod).
ALTER TABLE po_header ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;  -- 005_po_rebuild backfills off it
