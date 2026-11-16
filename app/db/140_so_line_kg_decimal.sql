-- 140_so_line_kg_decimal.sql — so_line.quantity_units (the SO line's total kg)
-- from INTEGER to NUMERIC(15,3).
--
-- WHY: the SO upload computes kg = qty × all_sku.uom (e.g. 8 packs × 0.075 kg =
-- 0.6 kg) and asyncpg truncates a float bound to an INTEGER column, so every line
-- under 1 kg was saved as 0 kg and every other line lost its decimals (460.8 →
-- 460). sync_fulfillment then copies that into so_fulfillment_v2.original_qty_kg,
-- which is what the SO page's "pending kg" and planning read. On 13 Nov 2026 the
-- NUTWORKS / AUSTRALIA MACADAMIA / Zorro 75–100 g lines (≈120 open lines) showed
-- 0 kg for this reason. so_fulfillment_v2.original_qty_kg is already NUMERIC.
--
-- SAFE: no view, rule or trigger depends on the column (checked on RDS 13 Nov
-- 2026); INTEGER → NUMERIC is a lossless widening; ~5.3k rows, so the rewrite
-- under the ACCESS EXCLUSIVE lock is brief. Re-running is a no-op.
--
-- DEPLOY ORDER: either. The backend now writes kg with 3 decimals; against the
-- old INTEGER column asyncpg truncates exactly as before, so nothing breaks
-- before this lands — lines just keep losing decimals until it does.
--
-- Existing truncated values are not recomputed here (the exact kg is lost); the
-- one-off scratch/fix_missing_kg.py fills the open lines that sit at 0 kg.

ALTER TABLE so_line
    ALTER COLUMN quantity_units TYPE NUMERIC(15,3) USING quantity_units::NUMERIC(15,3);

COMMENT ON COLUMN so_line.quantity_units IS
    'Total kg for the line (quantity × all_sku.uom, or the SO Book''s Alt. Units when the master has no kg per unit). NUMERIC(15,3) since migration 140.';
