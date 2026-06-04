-- =========================================================================
-- Migration 039: repair the job_card_material_consumption_v2 unique index
--
-- WHY:
--   app/modules/production/services/job_card_v2.py::upsert_consumption_lines
--   issues  ON CONFLICT (job_card_id, material_sku_name)  which requires the
--   unique index uq_jcmc_v2_jc_material (declared in 018_jc_accounting_v2.sql).
--   On production that index is MISSING, so every
--       POST /api/v1/production/job-cards-v2/{id}/outputs
--   500s with:
--       asyncpg.exceptions.InvalidColumnReferenceError: there is no unique or
--       exclusion constraint matching the ON CONFLICT specification
--
--   Root cause: migration 018 is not listed in scripts/migrate.py SQL_FILES
--   (the runner skips 016–029) and was hand-applied. CREATE UNIQUE INDEX
--   fails when duplicate (job_card_id, material_sku_name) rows already exist,
--   and the IF NOT EXISTS guard means the failed creation is never retried —
--   leaving the index permanently absent.
--
-- WHAT (idempotent, safe to re-run):
--   1. de-duplicate (job_card_id, material_sku_name), keeping the most recent
--      recorded_at — this matches the app's upsert semantics, which OVERWRITE
--      actual_consumed_qty on re-save (ON CONFLICT DO UPDATE, not SUM);
--   2. (re)create the unique index;
--   3. verify.
--
--   Wrapped in a to_regclass guard so it is a harmless no-op on a database
--   that has not yet created the table (e.g. a fresh bootstrap that has not
--   run 018).
-- =========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.job_card_material_consumption_v2') IS NULL THEN
        RAISE NOTICE 'job_card_material_consumption_v2 absent — skipping 039';
        RETURN;
    END IF;

    -- 1. Collapse duplicates. Keep the newest row per
    --    (job_card_id, material_sku_name); tie-break on the larger
    --    consumption_id when recorded_at ties.
    DELETE FROM job_card_material_consumption_v2 a
    USING job_card_material_consumption_v2 b
    WHERE a.job_card_id       = b.job_card_id
      AND a.material_sku_name  = b.material_sku_name
      AND (
              a.recorded_at <  b.recorded_at
           OR (a.recorded_at = b.recorded_at AND a.consumption_id < b.consumption_id)
          );

    -- 2. Create the unique index ON CONFLICT relies on. Mirrors the
    --    declaration in 018_jc_accounting_v2.sql so the two converge.
    CREATE UNIQUE INDEX IF NOT EXISTS uq_jcmc_v2_jc_material
        ON job_card_material_consumption_v2(job_card_id, material_sku_name);
END $$;

COMMIT;

-- ── Verification ─────────────────────────────────────────────────────────
-- (1) index must now exist:
SELECT indexname
FROM   pg_indexes
WHERE  indexname = 'uq_jcmc_v2_jc_material';

-- (2) zero duplicate groups must remain:
SELECT job_card_id, material_sku_name, COUNT(*) AS dupes
FROM   job_card_material_consumption_v2
GROUP  BY job_card_id, material_sku_name
HAVING COUNT(*) > 1;
