-- PO migration: restructure boxes into sections
-- Must run AFTER po_schema.sql (po_section table must exist)
-- Safe to re-run — each block checks before acting

-- 1. Add section_number column to po_box if missing
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='po_box' AND column_name='section_number') THEN
        -- Add column as nullable first
        ALTER TABLE po_box ADD COLUMN section_number INT;

        -- Migrate existing boxes: create a default section (section_number=1) per (transaction_no, line_number)
        INSERT INTO po_section (transaction_no, line_number, section_number, lot_number)
        SELECT DISTINCT transaction_no, line_number, 1, NULL
        FROM po_box
        ON CONFLICT DO NOTHING;

        -- Set all existing boxes to section_number=1
        UPDATE po_box SET section_number = 1 WHERE section_number IS NULL;

        -- Make column NOT NULL
        ALTER TABLE po_box ALTER COLUMN section_number SET NOT NULL;
    END IF;
END
$$;

-- 2. Drop old manufacturing_date and expiry_date from po_line if they exist
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='po_line' AND column_name='manufacturing_date') THEN
        ALTER TABLE po_line DROP COLUMN manufacturing_date;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='po_line' AND column_name='expiry_date') THEN
        ALTER TABLE po_line DROP COLUMN expiry_date;
    END IF;
END
$$;
