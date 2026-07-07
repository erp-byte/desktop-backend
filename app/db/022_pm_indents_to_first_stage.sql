-- =========================================================================
-- Migration 022: PM indents belong on stage 1 (alongside RM)
--
-- Spec shift (2026-05): both RM and PM issuance happen on the first
-- stage of a plan-line chain. The chain's downstream stages consume
-- upstream WIP via dispatch_to_next; only stage 1 receives fresh
-- material from stores.
--
-- Previously, job_card_engine_v2 materialised RM rows on the first
-- stage and PM rows on the last stage. With the unified issuance
-- model, any existing PM rows that landed on a non-stage-1 JC need to
-- be relocated to the corresponding stage-1 JC of the same plan line.
--
-- Strategy:
--   * Find PM rows on JCs whose step_number > 1.
--   * For each, find the stage-1 JC of the same plan_line_id.
--   * Move the row over only when the stage-1 JC doesn't already have
--     a row for the same material_sku_name — avoids creating duplicates
--     that would confuse the operator.
--   * Rows that can't be safely moved (no stage-1 target, or a
--     stage-1 row already exists for that material) are left in place;
--     they're surfaced via the post-migration verification SELECT.
--
-- Idempotent. Safe to re-run — once stage 1 has every material the
-- legacy rows belong to, subsequent runs are no-ops because the
-- NOT EXISTS clause matches.
-- =========================================================================

BEGIN;

WITH movable AS (
    SELECT pmi.pm_indent_id,
           first_jc.job_card_id AS new_jc_id,
           pmi.job_card_id      AS old_jc_id,
           pmi.material_sku_name
    FROM   job_card_pm_indent_v2 pmi
    JOIN   job_card_v2 cur
            ON cur.job_card_id = pmi.job_card_id
    JOIN   LATERAL (
              SELECT job_card_id
              FROM   job_card_v2
              WHERE  plan_line_id = cur.plan_line_id
                AND  step_number  = 1
                AND  deleted_at IS NULL
              LIMIT 1
           ) first_jc ON TRUE
    WHERE  cur.step_number > 1
      AND  cur.deleted_at IS NULL
      AND  NOT EXISTS (
            SELECT 1 FROM job_card_pm_indent_v2 dupe
            WHERE  dupe.job_card_id      = first_jc.job_card_id
              AND  dupe.material_sku_name = pmi.material_sku_name
           )
)
UPDATE job_card_pm_indent_v2 pmi
SET    job_card_id = movable.new_jc_id
FROM   movable
WHERE  pmi.pm_indent_id = movable.pm_indent_id;

COMMIT;

-- ── Post-migration verification ─────────────────────────────────────────
-- Any PM rows still on a non-stage-1 JC after this migration either:
--   (a) had no stage-1 sibling on the same plan_line, or
--   (b) the stage-1 sibling already had a row for the same material.
-- Both cases warrant manual review.

SELECT pmi.pm_indent_id,
       pmi.job_card_id,
       cur.step_number,
       cur.plan_line_id,
       pmi.material_sku_name,
       pmi.reqd_qty,
       pmi.issued_qty
FROM   job_card_pm_indent_v2 pmi
JOIN   job_card_v2 cur ON cur.job_card_id = pmi.job_card_id
WHERE  cur.step_number > 1
  AND  cur.deleted_at IS NULL
ORDER  BY cur.plan_line_id, cur.step_number, pmi.material_sku_name;
