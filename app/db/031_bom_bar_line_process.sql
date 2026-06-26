-- Migration: add bar_line_process column to bom_header
-- Source: docs/FG_Master_Completion (1) (1).xlsx column F
-- New: separate raw text capturing the detailed bar-line process flow
-- (independent of the existing process_category-derived bom_process_route).

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'bom_header' AND column_name = 'bar_line_process'
    ) THEN
        ALTER TABLE bom_header ADD COLUMN bar_line_process TEXT;
    END IF;
END
$$;
