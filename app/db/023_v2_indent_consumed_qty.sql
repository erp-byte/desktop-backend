-- =========================================================================
-- Migration 023: per-BOM-line consumption + lineage on v2 indent tables
--
-- The v2 RM / PM indent tables were created in migration 017 without
-- two columns the Output & Accounting tab depends on:
--
--   * consumed_qty NUMERIC(15,3) NULL
--       Per-row "how much of this material was actually consumed in
--       producing the JC's output". Written by the save-output endpoint;
--       read by the detail endpoint so the Material Consumption rows
--       round-trip the operator's prior entry.
--
--   * bom_line_id INT NULL REFERENCES bom_line(bom_line_id)
--       Direct FK from the per-JC indent row back to the BOM catalog
--       row it was materialised from. Lets the save endpoint dispatch
--       a per-line consumption update by bom_line_id (the same id that
--       the UI's BOM dropdown selects) without ambiguous joins on
--       material_sku_name.
--
-- Both columns mirror v1's job_card_rm_indent / job_card_pm_indent
-- (added by an earlier migration in production_migrate.sql) so the
-- per-line accounting model is symmetric between v1 and v2.
--
-- Backfill: existing indent rows have NULL bom_line_id. We resolve by
-- joining bom_line on (bom_id, LOWER(material_sku_name)) — the same
-- match the v1 backfill uses. Rows whose name doesn't match the BOM
-- (legacy hand-entered indents) are left NULL and surfaced in the
-- post-migration verification SELECT.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- ── RM indent ──────────────────────────────────────────────────────────
ALTER TABLE job_card_rm_indent_v2
    ADD COLUMN IF NOT EXISTS consumed_qty NUMERIC(15,3);

ALTER TABLE job_card_rm_indent_v2
    ADD COLUMN IF NOT EXISTS bom_line_id  INT
        REFERENCES bom_line(bom_line_id);

CREATE INDEX IF NOT EXISTS idx_rm_indent_v2_bom_line
    ON job_card_rm_indent_v2(bom_line_id);

-- Backfill bom_line_id from bom_line catalog by name match (case-insensitive).
WITH rm_resolved AS (
    SELECT ri.rm_indent_id,
           (SELECT bl.bom_line_id
              FROM bom_line bl
              JOIN job_card_v2 jc ON jc.bom_id = bl.bom_id
              WHERE jc.job_card_id = ri.job_card_id
                AND LOWER(bl.material_sku_name) = LOWER(ri.material_sku_name)
                AND LOWER(bl.item_type) = 'rm'
              LIMIT 1) AS resolved_id
    FROM job_card_rm_indent_v2 ri
    WHERE ri.bom_line_id IS NULL
)
UPDATE job_card_rm_indent_v2 ri
SET    bom_line_id = rm_resolved.resolved_id
FROM   rm_resolved
WHERE  ri.rm_indent_id = rm_resolved.rm_indent_id
  AND  rm_resolved.resolved_id IS NOT NULL;

-- ── PM indent ──────────────────────────────────────────────────────────
ALTER TABLE job_card_pm_indent_v2
    ADD COLUMN IF NOT EXISTS consumed_qty NUMERIC(15,3);

ALTER TABLE job_card_pm_indent_v2
    ADD COLUMN IF NOT EXISTS bom_line_id  INT
        REFERENCES bom_line(bom_line_id);

CREATE INDEX IF NOT EXISTS idx_pm_indent_v2_bom_line
    ON job_card_pm_indent_v2(bom_line_id);

WITH pm_resolved AS (
    SELECT pi.pm_indent_id,
           (SELECT bl.bom_line_id
              FROM bom_line bl
              JOIN job_card_v2 jc ON jc.bom_id = bl.bom_id
              WHERE jc.job_card_id = pi.job_card_id
                AND LOWER(bl.material_sku_name) = LOWER(pi.material_sku_name)
                AND LOWER(bl.item_type) = 'pm'
              LIMIT 1) AS resolved_id
    FROM job_card_pm_indent_v2 pi
    WHERE pi.bom_line_id IS NULL
)
UPDATE job_card_pm_indent_v2 pi
SET    bom_line_id = pm_resolved.resolved_id
FROM   pm_resolved
WHERE  pi.pm_indent_id = pm_resolved.pm_indent_id
  AND  pm_resolved.resolved_id IS NOT NULL;

COMMIT;

-- ── Post-migration verification ─────────────────────────────────────────
-- Indent rows whose bom_line_id is still NULL — these need manual cleanup
-- because their material_sku_name doesn't match any line in the JC's BOM
-- (typo, renamed material, etc).
SELECT 'rm' AS kind, rm_indent_id AS indent_id, job_card_id,
       material_sku_name, uom
  FROM job_card_rm_indent_v2 WHERE bom_line_id IS NULL
UNION ALL
SELECT 'pm' AS kind, pm_indent_id AS indent_id, job_card_id,
       material_sku_name, uom
  FROM job_card_pm_indent_v2 WHERE bom_line_id IS NULL
ORDER BY kind, job_card_id;
