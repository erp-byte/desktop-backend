-- Migration: ensure tables match the app schema
-- Safe to re-run — each block checks before acting

-- 1. Ensure so_header columns are nullable where needed
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_header' AND column_name='so_number') THEN
        ALTER TABLE so_header ALTER COLUMN so_number DROP NOT NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_header' AND column_name='so_date') THEN
        ALTER TABLE so_header ALTER COLUMN so_date DROP NOT NULL;
    END IF;
END
$$;

-- 2. Ensure log_edit.changed_by is nullable
ALTER TABLE log_edit ALTER COLUMN changed_by DROP NOT NULL;

-- 3. Add 'failed' to extraction_status enum if missing
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum e
        JOIN pg_type t ON e.enumtypid = t.oid
        WHERE t.typname = 'e_extraction_status' AND e.enumlabel = 'failed'
    ) THEN
        ALTER TYPE e_extraction_status ADD VALUE 'failed';
    END IF;
END
$$;

-- 4. Ensure so_line columns that may be NULL
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='quantity_units') THEN
        ALTER TABLE so_line ALTER COLUMN quantity_units DROP NOT NULL;
    END IF;
END
$$;

-- 5. Add Excel ingestion columns to so_header
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_header' AND column_name='customer_name') THEN
        ALTER TABLE so_header ADD COLUMN customer_name TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_header' AND column_name='common_customer_name') THEN
        ALTER TABLE so_header ADD COLUMN common_customer_name TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_header' AND column_name='company') THEN
        ALTER TABLE so_header ADD COLUMN company TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_header' AND column_name='voucher_type') THEN
        ALTER TABLE so_header ADD COLUMN voucher_type TEXT;
    END IF;
END
$$;

-- 6. Add Excel ingestion columns to so_line
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='grp_code') THEN
        ALTER TABLE so_line ADD COLUMN grp_code TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='igst_amount') THEN
        ALTER TABLE so_line ADD COLUMN igst_amount NUMERIC;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='sgst_amount') THEN
        ALTER TABLE so_line ADD COLUMN sgst_amount NUMERIC;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='cgst_amount') THEN
        ALTER TABLE so_line ADD COLUMN cgst_amount NUMERIC;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='total_amount_inr') THEN
        ALTER TABLE so_line ADD COLUMN total_amount_inr NUMERIC;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='rate_type') THEN
        ALTER TABLE so_line ADD COLUMN rate_type TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='quantity') THEN
        ALTER TABLE so_line ADD COLUMN quantity NUMERIC;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='item_type') THEN
        ALTER TABLE so_line ADD COLUMN item_type TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='item_category') THEN
        ALTER TABLE so_line ADD COLUMN item_category TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='sub_category') THEN
        ALTER TABLE so_line ADD COLUMN sub_category TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='item_description') THEN
        ALTER TABLE so_line ADD COLUMN item_description TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='sales_group') THEN
        ALTER TABLE so_line ADD COLUMN sales_group TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='match_score') THEN
        ALTER TABLE so_line ADD COLUMN match_score NUMERIC;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='match_source') THEN
        ALTER TABLE so_line ADD COLUMN match_source TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='sku_name') THEN
        ALTER TABLE so_line ADD COLUMN sku_name TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='uom') THEN
        ALTER TABLE so_line ADD COLUMN uom TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='rate_inr') THEN
        ALTER TABLE so_line ADD COLUMN rate_inr NUMERIC;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='so_line' AND column_name='amount_inr') THEN
        ALTER TABLE so_line ADD COLUMN amount_inr NUMERIC;
    END IF;
END
$$;

-- 7. Create GST reconciliation table
CREATE TABLE IF NOT EXISTS so_gst_reconciliation (
    recon_id              SERIAL PRIMARY KEY,
    so_line_id            INT NOT NULL REFERENCES so_line(so_line_id),
    so_id                 INT NOT NULL REFERENCES so_header(so_id),
    expected_gst_rate     NUMERIC,
    actual_gst_rate       NUMERIC,
    expected_gst_amount   NUMERIC,
    actual_gst_amount     NUMERIC,
    gst_difference        NUMERIC,
    gst_type              TEXT,
    gst_type_valid        BOOLEAN,
    sgst_cgst_equal       BOOLEAN,
    total_with_gst_valid  BOOLEAN,
    uom_match             BOOLEAN,
    item_type_flag        TEXT,
    rate_type             TEXT,
    status                TEXT NOT NULL DEFAULT 'ok',
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_gst_recon_so ON so_gst_reconciliation(so_id);
CREATE INDEX IF NOT EXISTS idx_gst_recon_status ON so_gst_reconciliation(status);
