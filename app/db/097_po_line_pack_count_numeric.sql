-- 097_po_line_pack_count_numeric.sql — po_line.pack_count INT -> NUMERIC(15,3).
--
-- WHY: Tally's "Quantity" column is a decimal measure (kg), not a count of
-- packs. po_schema.sql:56 declared it INT and parser.py truncated with int(),
-- so every fractional quantity lost its decimal and anything under 1 became a
-- literal 0:
--     Rose Petals      20.8   -> 20      (CF/PO/2026-27/01231)
--     Labour Charges    0.96  ->  0
-- ~12% of lines in the Jan-Mar workbook carry a fractional quantity.
--
-- NOTE: the Supabase database this app points at ALREADY reports
-- pack_count as numeric(15,3) — it drifted from po_schema.sql at some earlier
-- point. This migration is therefore a no-op there and exists so that a
-- database built fresh from app/db/ matches production. po_schema.sql is
-- updated in the same change for the same reason.
--
-- IDEMPOTENT: guarded on the current type, so re-running does nothing.
-- scripts/migrate.py re-executes every file on every deploy.
--
-- COST: widening INT -> NUMERIC rewrites the table. po_line is small (410 rows
-- today); on a large table this would need a maintenance window.
--
-- SAFE: INT -> NUMERIC(15,3) is a widening conversion. Every existing integer
-- value is representable, so no data is lost and no USING clause is needed.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'po_line'
           AND column_name = 'pack_count'
           AND data_type = 'integer'
    ) THEN
        ALTER TABLE po_line ALTER COLUMN pack_count TYPE NUMERIC(15,3);
        RAISE NOTICE 'po_line.pack_count widened to NUMERIC(15,3)';
    ELSE
        RAISE NOTICE 'po_line.pack_count already non-integer — nothing to do';
    END IF;
END $$;
