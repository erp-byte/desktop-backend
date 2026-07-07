-- =========================================================================
-- Migration 024: bom_line_id FK on job_card_material_consumption_v2
--
-- job_card_material_consumption_v2 (migration 018) is the per-stage,
-- per-material consumption ledger. It already supports input_kind ∈
-- {RM, SFG, WIP, PM} and a UOM CHECK across the full universal list,
-- but the line is identified only by (job_card_id, material_sku_name)
-- — fragile when two BOMs share a material name with different units,
-- or when a SKU is renamed.
--
-- This migration adds a direct FK back to bom_line so the save-output
-- endpoint can dispatch each per-line entry by bom_line_id (the same
-- id the UI's dropdown selects), and surfaces stage-1 / non-stage-1
-- attribution unambiguously.
--
-- Backfill: existing rows have NULL bom_line_id. We resolve by joining
-- bom_line on (the JC's bom_id, LOWER(material_sku_name), LOWER(item_type)).
-- Rows that fail to match are left NULL and surfaced in the post-migration
-- verification SELECT.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

ALTER TABLE job_card_material_consumption_v2
    ADD COLUMN IF NOT EXISTS bom_line_id INT
        REFERENCES bom_line(bom_line_id);

CREATE INDEX IF NOT EXISTS idx_jcmc_v2_bom_line
    ON job_card_material_consumption_v2(bom_line_id);

-- Backfill bom_line_id by name+kind match on the JC's BOM. input_kind
-- maps directly to bom_line.item_type ('rm' / 'pm'); SFG/WIP rows
-- represent upstream dispatches, not BOM lines, so they stay NULL by
-- design.
WITH resolved AS (
    SELECT mc.consumption_id,
           (SELECT bl.bom_line_id
              FROM bom_line bl
              JOIN job_card_v2 jc ON jc.bom_id = bl.bom_id
              WHERE jc.job_card_id = mc.job_card_id
                AND LOWER(bl.material_sku_name) = LOWER(mc.material_sku_name)
                AND LOWER(bl.item_type)         = LOWER(mc.input_kind)
              LIMIT 1) AS resolved_id
    FROM   job_card_material_consumption_v2 mc
    WHERE  mc.bom_line_id IS NULL
      AND  mc.input_kind IN ('RM', 'PM')
)
UPDATE job_card_material_consumption_v2 mc
SET    bom_line_id = resolved.resolved_id
FROM   resolved
WHERE  mc.consumption_id = resolved.consumption_id
  AND  resolved.resolved_id IS NOT NULL;

COMMIT;

-- ── Post-migration verification ─────────────────────────────────────────
-- RM / PM rows still missing bom_line_id after the backfill.
-- SFG / WIP rows are expected to be NULL (they reference dispatches,
-- not BOM lines) and are omitted from this list.
SELECT consumption_id, job_card_id, material_sku_name, input_kind, uom,
       actual_consumed_qty
  FROM job_card_material_consumption_v2
 WHERE bom_line_id IS NULL
   AND input_kind IN ('RM', 'PM')
 ORDER BY job_card_id, input_kind, material_sku_name;
