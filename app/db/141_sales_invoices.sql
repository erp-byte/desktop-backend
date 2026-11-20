-- 141: Sales invoices (SO Creation's second upload: the Tally Sales Register).
--
-- One SO can have many invoices: sales_invoice.so_number references
-- so_header(so_number), and an invoice number is unique (primary key).
-- sales_invoice_line holds each invoice's items and the SO line each one was
-- dispatched against, so pending = ordered - invoiced can be worked out per
-- SO line (the Pending Orders export).
--
-- Safe to re-run. Apply on RDS with scratch/apply_migration_141.py.

-- 1. so_number must be unique for invoices to reference it. Checked on RDS
--    2026-11-19: 956 SOs, no NULL and no duplicate so_number (also ignoring
--    case), and the upload code already refuses a second SO with the same number.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'so_header'::regclass AND conname = 'so_header_so_number_key') THEN
        ALTER TABLE so_header ADD CONSTRAINT so_header_so_number_key UNIQUE (so_number);
    END IF;
END $$;

-- 2. Invoices: one row per Tally voucher (cancelled ones too, flagged).
CREATE TABLE IF NOT EXISTS sales_invoice (
    invoice_no         VARCHAR PRIMARY KEY,                  -- Voucher No. (CND/26-27/5548)
    so_number          VARCHAR REFERENCES so_header(so_number)
                           ON UPDATE CASCADE ON DELETE SET NULL,  -- NULL: quotes no SO in the ERP
    invoice_date       DATE NOT NULL,
    voucher_type       TEXT,
    party              TEXT,
    buyer              TEXT,
    buyer_address      TEXT,
    consignee          TEXT,
    consignee_address  TEXT,
    reference          TEXT,                                 -- Voucher Ref. No. as written
    narration          TEXT,
    dispatch_from      TEXT,                                 -- site read off the narration
    cartons            INT,
    replaces_invoice   VARCHAR,                              -- "Against Old Invoice No.: ..."
    cancelled          BOOLEAN NOT NULL DEFAULT FALSE,
    quantity           NUMERIC(15,3),
    alt_units          NUMERIC(15,3),
    value              NUMERIC(15,2),
    gross_total        NUMERIC(15,2),
    ledgers            JSONB NOT NULL DEFAULT '{}'::jsonb,   -- tax / charge columns by header
    source_file        TEXT,
    uploaded_by        TEXT,
    uploaded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sales_invoice_so_number ON sales_invoice (so_number);
CREATE INDEX IF NOT EXISTS idx_sales_invoice_date ON sales_invoice (invoice_date);

-- 3. Invoice items, each tied (when it could be matched) to the SO line it fulfils.
CREATE TABLE IF NOT EXISTS sales_invoice_line (
    invoice_no         VARCHAR NOT NULL REFERENCES sales_invoice(invoice_no) ON DELETE CASCADE,
    line_no            INT NOT NULL,
    particulars        TEXT NOT NULL,                        -- article as invoiced
    so_line_id         BIGINT REFERENCES so_line(so_line_id) ON DELETE SET NULL,
    matched_article    TEXT,                                 -- master-list article
    match_score        NUMERIC(5,3),
    quantity           NUMERIC(15,3),
    alt_units          NUMERIC(15,3),                        -- Tally Alt. Units (kg)
    kg                 NUMERIC(15,3),
    kg_source          TEXT,                                 -- alt_units | master_uom
    rate               NUMERIC(15,3),
    value              NUMERIC(15,2),
    PRIMARY KEY (invoice_no, line_no)
);
CREATE INDEX IF NOT EXISTS idx_sales_invoice_line_so_line ON sales_invoice_line (so_line_id);

COMMENT ON TABLE sales_invoice IS
    'Invoices raised (Tally Sales Register), uploaded on SO Creation. One SO -> many invoices via so_number.';
COMMENT ON COLUMN sales_invoice_line.so_line_id IS
    'The SO line this item was dispatched against (article matched within the invoice''s SO); NULL when not matched.';
