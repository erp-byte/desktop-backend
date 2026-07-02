-- 070_customer_returns.sql
-- Customer-Returns module (source "RTV" = customer returns, CR- ids).
-- Per-company header/lines/boxes (cfpl_/cdpl_) + a GLOBAL box_edit_logs audit
-- table. Natural keys: header PK = rtv_id ('CR-YYYYMMDDHHMMSS'); lines keyed by
-- (rtv_id, item_description); boxes keyed by (rtv_id, article_description,
-- box_number). No sequential id. Additive + idempotent (safe to re-run).

-- ── CFPL ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cfpl_customer_return_header (
    rtv_id           TEXT PRIMARY KEY,               -- 'CR-YYYYMMDDHHMMSS'
    rtv_date         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    factory_unit     TEXT NOT NULL,
    customer         TEXT NOT NULL,
    invoice_number   TEXT,
    challan_no       TEXT,
    dn_no            TEXT,
    conversion       DOUBLE PRECISION DEFAULT 0,
    sales_poc        TEXT,
    sales_poc_email  TEXT,
    business_head    TEXT,
    remark           TEXT,
    vehicle_number   TEXT,
    transporter_name TEXT,
    driver_name      TEXT,
    inward_manager   TEXT,
    status           TEXT NOT NULL DEFAULT 'Pending', -- Pending/Submitted/Approved/Rejected/On Hold
    created_by       TEXT,
    created_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cfpl_cr_header_status ON cfpl_customer_return_header(status);

CREATE TABLE IF NOT EXISTS cfpl_customer_return_lines (
    rtv_id           TEXT NOT NULL REFERENCES cfpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    item_description TEXT NOT NULL,
    material_type    TEXT NOT NULL,
    item_category    TEXT NOT NULL,
    sub_category     TEXT NOT NULL,
    uom              TEXT NOT NULL,
    qty              INTEGER NOT NULL DEFAULT 0,
    rate             DOUBLE PRECISION NOT NULL DEFAULT 0,
    value            DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_weight       DOUBLE PRECISION NOT NULL DEFAULT 0,
    carton_weight    DOUBLE PRECISION NOT NULL DEFAULT 0,
    lot_number       TEXT,
    item_mark        TEXT,
    spl_remarks      TEXT,
    vakkal           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, item_description)
);
CREATE INDEX IF NOT EXISTS idx_cfpl_cr_lines_rtv ON cfpl_customer_return_lines(rtv_id);

CREATE TABLE IF NOT EXISTS cfpl_customer_return_boxes (
    rtv_id              TEXT NOT NULL REFERENCES cfpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    article_description TEXT NOT NULL,
    box_number          INTEGER NOT NULL,
    box_id              TEXT,                          -- NULL until Print
    uom                 TEXT,
    conversion          TEXT,
    lot_number          TEXT,
    item_mark           TEXT,
    spl_remarks         TEXT,
    vakkal              TEXT,
    net_weight          NUMERIC(18,3) NOT NULL DEFAULT 0,
    gross_weight        NUMERIC(18,3) NOT NULL DEFAULT 0,
    count               INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, article_description, box_number)
);
CREATE INDEX IF NOT EXISTS idx_cfpl_cr_boxes_rtv ON cfpl_customer_return_boxes(rtv_id);

-- ── CDPL (identical shape) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cdpl_customer_return_header (
    rtv_id           TEXT PRIMARY KEY,
    rtv_date         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    factory_unit     TEXT NOT NULL,
    customer         TEXT NOT NULL,
    invoice_number   TEXT,
    challan_no       TEXT,
    dn_no            TEXT,
    conversion       DOUBLE PRECISION DEFAULT 0,
    sales_poc        TEXT,
    sales_poc_email  TEXT,
    business_head    TEXT,
    remark           TEXT,
    vehicle_number   TEXT,
    transporter_name TEXT,
    driver_name      TEXT,
    inward_manager   TEXT,
    status           TEXT NOT NULL DEFAULT 'Pending',
    created_by       TEXT,
    created_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cdpl_cr_header_status ON cdpl_customer_return_header(status);

CREATE TABLE IF NOT EXISTS cdpl_customer_return_lines (
    rtv_id           TEXT NOT NULL REFERENCES cdpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    item_description TEXT NOT NULL,
    material_type    TEXT NOT NULL,
    item_category    TEXT NOT NULL,
    sub_category     TEXT NOT NULL,
    uom              TEXT NOT NULL,
    qty              INTEGER NOT NULL DEFAULT 0,
    rate             DOUBLE PRECISION NOT NULL DEFAULT 0,
    value            DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_weight       DOUBLE PRECISION NOT NULL DEFAULT 0,
    carton_weight    DOUBLE PRECISION NOT NULL DEFAULT 0,
    lot_number       TEXT,
    item_mark        TEXT,
    spl_remarks      TEXT,
    vakkal           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, item_description)
);
CREATE INDEX IF NOT EXISTS idx_cdpl_cr_lines_rtv ON cdpl_customer_return_lines(rtv_id);

CREATE TABLE IF NOT EXISTS cdpl_customer_return_boxes (
    rtv_id              TEXT NOT NULL REFERENCES cdpl_customer_return_header(rtv_id) ON DELETE CASCADE,
    article_description TEXT NOT NULL,
    box_number          INTEGER NOT NULL,
    box_id              TEXT,
    uom                 TEXT,
    conversion          TEXT,
    lot_number          TEXT,
    item_mark           TEXT,
    spl_remarks         TEXT,
    vakkal              TEXT,
    net_weight          NUMERIC(18,3) NOT NULL DEFAULT 0,
    gross_weight        NUMERIC(18,3) NOT NULL DEFAULT 0,
    count               INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ,
    PRIMARY KEY (rtv_id, article_description, box_number)
);
CREATE INDEX IF NOT EXISTS idx_cdpl_cr_boxes_rtv ON cdpl_customer_return_boxes(rtv_id);

-- ── Global box-edit audit log (append-only, no surrogate PK) ─────────────
CREATE TABLE IF NOT EXISTS box_edit_logs (
    email_id       TEXT,
    description    TEXT,
    transaction_no TEXT,   -- the rtv_id string
    box_id         TEXT,
    field_name     TEXT,
    old_value      TEXT,
    new_value      TEXT,
    edited_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_box_edit_logs_box ON box_edit_logs(box_id, field_name);
