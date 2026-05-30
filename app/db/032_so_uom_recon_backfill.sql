-- 032_so_uom_recon_backfill.sql
--
-- Backfill so_gst_reconciliation.uom_match for rows the previous reconciliation
-- skipped because the Excel UOM cell was an alphabetical label (e.g. "PCS",
-- "CTN") that couldn't be compared numerically against the master's kg-per-unit
-- factor (all_sku.uom). The new logic verifies the math relationship instead:
--
--     so_line.quantity_units (kg)  /  matched_uom (kg-per-unit)
--         ==  so_line.quantity (pack count)        (± 0.5 packs of slack)
--
-- Rows updated:
--   * uom_match flipped from NULL → TRUE when the math checks out
--   * uom_match flipped from NULL → FALSE + status bumped to 'warning' +
--     diagnostic note appended, when the math is off
--
-- Rows left alone:
--   * Numeric Excel UOMs — already compared by the original logic.
--   * Rows without a matched master (matched_uom IS NULL).
--   * Rows where quantity / quantity_units / matched_uom are zero or null
--     (no check is possible without all three).
--
-- Idempotent: re-running finds no remaining `uom_match IS NULL` rows that
-- match the predicate, so it is a no-op on subsequent runs.

UPDATE so_gst_reconciliation gr
SET
    uom_match = (abs(sl.quantity_units::numeric / gr.matched_uom - sl.quantity) < 0.5),
    status = CASE
        WHEN abs(sl.quantity_units::numeric / gr.matched_uom - sl.quantity) < 0.5
            THEN gr.status
        WHEN gr.status = 'ok' THEN 'warning'
        ELSE gr.status
    END,
    notes = CASE
        WHEN abs(sl.quantity_units::numeric / gr.matched_uom - sl.quantity) < 0.5
            THEN gr.notes
        ELSE concat_ws(
            '; ',
            NULLIF(gr.notes, ''),
            'UOM check: ' || sl.quantity_units || ' kg / ' || gr.matched_uom
                || ' = ' || round(sl.quantity_units::numeric / gr.matched_uom, 2)
                || ' units (expected ' || sl.quantity || ')'
        )
    END
FROM so_line sl
WHERE gr.so_line_id = sl.so_line_id
  AND gr.uom_match IS NULL
  AND gr.matched_uom IS NOT NULL
  AND gr.matched_uom > 0
  AND sl.uom IS NOT NULL
  AND length(trim(sl.uom)) > 0
  -- Alphabetical labels only: skip rows whose UOM cell is itself numeric
  -- (those were handled correctly by the original reconciliation).
  AND sl.uom !~ '^[+-]?[0-9]+(\.[0-9]+)?$'
  AND sl.quantity IS NOT NULL
  AND sl.quantity > 0
  AND sl.quantity_units IS NOT NULL
  AND sl.quantity_units > 0;
