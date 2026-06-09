-- =========================================================================
-- Migration 046: backfill job_card_output_v2.batch_id for historical rows
-- that record_output wrote without the batch_id tag.
--
-- WHY:
--   record_output (services/job_card_v2.py) never accepted or persisted
--   batch_id, so every output row written via POST /outputs landed with
--   batch_id = NULL — even after migration 036 added the column. Sibling
--   tables (job_card_material_consumption_v2 / job_card_byproducts_v2 /
--   job_card_balance_material_v2) all carry the correct batch_id, so the
--   data is consistent everywhere EXCEPT job_card_output_v2.
--
--   Symptom (user-visible): JC form re-opens with FG Actual Kg / FG
--   Actual Units / Process Loss empty even though the values persisted.
--   The frontend's batchScopedDefaults fallback joins the latest output
--   row to the selected batch by batch_id; a NULL filters the row out, so
--   the form has no value to display.
--
-- WHAT (idempotent):
--   Map each null-batch_id output row to the JC's batch that was OPEN at
--   the output's recorded_at:
--       started_at <= recorded_at AND (closed_at IS NULL OR closed_at >= recorded_at)
--   If multiple batches matched (multi-batch JCs since migration 042),
--   prefer the one whose batch_number is highest at that timestamp —
--   matches the auto-pick heuristic the frontend uses.
--   If NO batch matched (output rows pre-dating any batch on the JC, or
--   JCs that never had a batch), leave batch_id NULL.
--
--   Companion service+router fix in this PR makes new writes correct.
-- =========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.job_card_output_v2') IS NULL THEN
        RAISE NOTICE 'job_card_output_v2 absent — skipping backfill';
        RETURN;
    END IF;

    -- Assign each null-batch_id output to the batch open at its recorded_at.
    -- Use DISTINCT ON to pick a single batch per output deterministically.
    UPDATE job_card_output_v2 o
       SET batch_id = picked.batch_id
      FROM (
          SELECT DISTINCT ON (o2.output_id)
                 o2.output_id, b.batch_id
            FROM job_card_output_v2 o2
            JOIN job_card_batch_v2 b ON b.job_card_id = o2.job_card_id
           WHERE o2.batch_id IS NULL
             AND b.started_at <= o2.recorded_at
             AND (b.closed_at IS NULL OR b.closed_at >= o2.recorded_at)
           ORDER BY o2.output_id, b.batch_number DESC
      ) picked
     WHERE o.output_id = picked.output_id
       AND o.batch_id IS NULL;
END $$;

COMMIT;

-- ── Verification ──────────────────────────────────────────────────────
-- (1) Count remaining null-batch_id output rows (only ones with no
--     matching batch by timestamp). Acceptable if the JC genuinely had no
--     batch at the recorded time.
SELECT COUNT(*) AS remaining_null_batch_id_outputs
FROM   job_card_output_v2
WHERE  batch_id IS NULL;

-- (2) Spot-check a single JC — output.batch_id should now agree with
--     consumption_lines.batch_id.
-- SELECT o.output_id, o.batch_id AS output_batch,
--        (SELECT batch_id FROM job_card_material_consumption_v2 c
--          WHERE c.job_card_id = o.job_card_id LIMIT 1) AS consumption_batch
--   FROM job_card_output_v2 o
--  WHERE o.job_card_id = <jc>
--  ORDER BY o.recorded_at DESC;
