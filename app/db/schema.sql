CREATE TABLE IF NOT EXISTS so_header (
    so_id                  SERIAL PRIMARY KEY,
    so_number              TEXT,
    so_date                DATE,
    customer_name          TEXT,
    common_customer_name   TEXT,
    company                TEXT,
    voucher_type           TEXT,
    extraction_status      TEXT NOT NULL DEFAULT 'pending',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS so_line (
    so_line_id            SERIAL PRIMARY KEY,
    so_id                 INT NOT NULL REFERENCES so_header(so_id),
    line_number           INT NOT NULL,
    sku_name              TEXT,
    item_category         TEXT,
    sub_category          TEXT,
    uom                   TEXT,
    grp_code              TEXT,
    quantity              NUMERIC(15,3),
    quantity_units        INT,
    rate_inr              NUMERIC(15,3),
    rate_type             TEXT,
    amount_inr            NUMERIC(15,3),
    igst_amount           NUMERIC(15,3),
    sgst_amount           NUMERIC(15,3),
    cgst_amount           NUMERIC(15,3),
    total_amount_inr      NUMERIC(15,3),
    apmc_amount           NUMERIC(15,3),
    packing_amount        NUMERIC(15,3),
    freight_amount        NUMERIC(15,3),
    processing_amount     NUMERIC(15,3),
    item_type             TEXT,
    item_description      TEXT,
    sales_group           TEXT,
    match_score           NUMERIC(15,3),
    match_source          TEXT,
    release_mode          TEXT DEFAULT 'all_upfront',
    status                TEXT NOT NULL DEFAULT 'pending',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (so_id, line_number)
);

CREATE TABLE IF NOT EXISTS all_sku (
    sku_id          SERIAL PRIMARY KEY,
    particulars     TEXT NOT NULL,
    item_type       TEXT,
    item_group      TEXT,
    sub_group       TEXT,
    uom             NUMERIC(15,3),
    sale_group      TEXT,
    gst             NUMERIC(15,3),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_all_sku_particulars ON all_sku(particulars);

CREATE TABLE IF NOT EXISTS so_gst_reconciliation (
    recon_id              SERIAL PRIMARY KEY,
    so_line_id            INT NOT NULL REFERENCES so_line(so_line_id),
    so_id                 INT NOT NULL REFERENCES so_header(so_id),
    expected_gst_rate     NUMERIC(15,3),
    actual_gst_rate       NUMERIC(15,3),
    expected_gst_amount   NUMERIC(15,3),
    actual_gst_amount     NUMERIC(15,3),
    gst_difference        NUMERIC(15,3),
    gst_type              TEXT,
    gst_type_valid        BOOLEAN,
    sgst_cgst_equal       BOOLEAN,
    total_with_gst_valid  BOOLEAN,
    uom_match             BOOLEAN,
    item_type_flag        TEXT,
    rate_type             TEXT,
    matched_item_description TEXT,
    matched_item_type     TEXT,
    matched_item_category TEXT,
    matched_sub_category  TEXT,
    matched_sales_group   TEXT,
    matched_uom           NUMERIC(15,3),
    match_score           NUMERIC(15,3),
    status                TEXT NOT NULL DEFAULT 'ok',
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gst_recon_so ON so_gst_reconciliation(so_id);
CREATE INDEX IF NOT EXISTS idx_gst_recon_status ON so_gst_reconciliation(status);

CREATE TABLE IF NOT EXISTS log_edit (
    log_id       SERIAL PRIMARY KEY,
    table_name   TEXT NOT NULL,
    record_id    INT NOT NULL,
    field_name   TEXT,
    action       TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    changed_by   INT,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_id   TEXT,
    module       TEXT NOT NULL DEFAULT 'so_intake'
);

CREATE INDEX IF NOT EXISTS idx_log_edit_record ON log_edit(table_name, record_id);
CREATE INDEX IF NOT EXISTS idx_log_edit_changed_at ON log_edit(changed_at);
