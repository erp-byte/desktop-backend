-- Purchase Order tables (v6)

CREATE TABLE IF NOT EXISTS po_header (
    -- Purchase Team (from Excel upload)
    transaction_no          TEXT PRIMARY KEY,
    entity                  TEXT NOT NULL CHECK (entity IN ('cfpl', 'cdpl')),
    po_date                 DATE,
    voucher_type            TEXT,
    po_number               TEXT,
    order_reference_no      TEXT,
    narration               TEXT,
    vendor_supplier_name    TEXT,
    gross_total             NUMERIC(15,3),
    total_amount            NUMERIC(15,3),
    sgst_amount             NUMERIC(15,3),
    cgst_amount             NUMERIC(15,3),
    igst_amount             NUMERIC(15,3),
    round_off               NUMERIC(15,3),
    freight_transport_local         NUMERIC(15,3),
    apmc_tax                        NUMERIC(15,3),
    packing_charges                 NUMERIC(15,3),
    freight_transport_charges       NUMERIC(15,3),
    loading_unloading_charges       NUMERIC(15,3),
    other_charges_non_gst           NUMERIC(15,3),
    -- Stores Team (post-receiving)
    customer_party_name     TEXT,
    vehicle_number          TEXT,
    transporter_name        TEXT,
    lr_number               TEXT,
    source_location         TEXT,
    destination_location    TEXT,
    challan_number          TEXT,
    invoice_number          TEXT,
    grn_number              TEXT,
    system_grn_date         TIMESTAMPTZ,
    purchased_by            TEXT,
    inward_authority        TEXT,
    warehouse               TEXT,
    -- System
    status                  TEXT NOT NULL DEFAULT 'pending',
    approved_by             TEXT,
    approved_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_po_header_entity ON po_header(entity);
CREATE INDEX IF NOT EXISTS idx_po_header_po_date ON po_header(po_date);
CREATE INDEX IF NOT EXISTS idx_po_header_status ON po_header(status);

CREATE TABLE IF NOT EXISTS po_line (
    -- Purchase Team (from Excel + all_sku matching)
    transaction_no          TEXT NOT NULL REFERENCES po_header(transaction_no),
    line_number             INT NOT NULL,
    sku_name                TEXT,
    uom                     TEXT,
    pack_count              INT,
    po_weight               NUMERIC(15,3),
    rate                    NUMERIC(15,3),
    amount                  NUMERIC(15,3),
    particulars             TEXT,
    item_category           TEXT,
    sub_category            TEXT,
    item_type               TEXT,
    sales_group             TEXT,
    gst_rate                NUMERIC(15,3),
    match_score             NUMERIC(5,3),
    match_source            TEXT,
    -- Stores Team (post-receiving)
    carton_weight           NUMERIC(15,3),
    -- System
    status                  TEXT NOT NULL DEFAULT 'pending',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (transaction_no, line_number)
);

CREATE INDEX IF NOT EXISTS idx_po_line_sku ON po_line(sku_name);

CREATE TABLE IF NOT EXISTS po_section (
    -- Stores Team only — lot-level grouping per article
    transaction_no          TEXT NOT NULL REFERENCES po_header(transaction_no),
    line_number             INT NOT NULL,
    section_number          INT NOT NULL,
    lot_number              TEXT,
    box_count               INT,
    manufacturing_date      TEXT,
    expiry_date             TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (transaction_no, line_number, section_number),
    FOREIGN KEY (transaction_no, line_number) REFERENCES po_line(transaction_no, line_number)
);

CREATE INDEX IF NOT EXISTS idx_po_section_txn ON po_section(transaction_no);

CREATE TABLE IF NOT EXISTS po_box (
    -- Stores Team only
    box_id                  TEXT PRIMARY KEY,
    transaction_no          TEXT NOT NULL REFERENCES po_header(transaction_no),
    line_number             INT NOT NULL,
    section_number          INT NOT NULL,
    box_number              INT NOT NULL,
    net_weight              NUMERIC(15,3),
    gross_weight            NUMERIC(15,3),
    lot_number              TEXT,
    count                   INT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (transaction_no, line_number, section_number) REFERENCES po_section(transaction_no, line_number, section_number)
);

CREATE INDEX IF NOT EXISTS idx_po_box_txn ON po_box(transaction_no);
