-- =========================================================================
-- Migration 040: repair the job_card_byproducts_v2 expression unique index
--
-- WHY:
--   app/modules/production/services/jc_accounting_v2.py::save_byproducts
--   issues  ON CONFLICT (job_card_id, category, (COALESCE(material_name,'')))
--   which requires the expression unique index uq_byproducts_jc_cat_mat
--   (declared in 034_byproducts_material_attribution.sql). On production that
--   index is MISSING, so every output-save that records byproducts:
--       POST /api/v1/production/job-cards-v2/{id}/outputs
--   500s with:
--       asyncpg.exceptions.InvalidColumnReferenceError: there is no unique or
--       exclusion constraint matching the ON CONFLICT specification
--
--   Same root cause as 039: migration 034 is not in scripts/migrate.py
--   SQL_FILES (the runner skips it) and was only partially hand-applied — the
--   material_name / bom_line_id columns exist on prod (the INSERT references
--   them) but the unique index was never created. 034 also does NOT de-dupe
--   before CREATE UNIQUE INDEX, so re-running it would fail again if duplicate
--   rows exist.
--
-- WHAT (idempotent, safe to re-run):
--   1. de-duplicate (job_card_id, category, COALESCE(material_name,'')),
--      keeping the most recent recorded_at — matches save_byproducts' upsert
--      semantics (ON CONFLICT DO UPDATE overwrites, not SUM);
--   2. drop the stale single-key UNIQUE (job_card_id, category) left by 018,
--      so multi-article rows in one category can coexist (034's intent);
--   3. (re)create the expression unique index;
--   4. verify.
--
--   Guarded: no-op if the table is absent, or if the material_name column is
--   absent (a fresh DB that hasn't run 034 — it should run 034, not this).
-- =========================================================================

BEGIN;

DO $$
DECLARE
    c RECORD;
BEGIN
    IF to_regclass('public.job_card_byproducts_v2') IS NULL THEN
        RAISE NOTICE 'job_card_byproducts_v2 absent — skipping 040';
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'job_card_byproducts_v2' AND column_name = 'material_name'
    ) THEN
        RAISE NOTICE 'material_name column absent — run 034 first; skipping 040';
        RETURN;
    END IF;

    -- 1. Collapse duplicates on the expression key, keeping the newest row
    --    (recorded_at, tie-broken by the larger byproduct_id).
    DELETE FROM job_card_byproducts_v2 a
    USING job_card_byproducts_v2 b
    WHERE a.job_card_id = b.job_card_id
      AND a.category     = b.category
      AND COALESCE(a.material_name, '') = COALESCE(b.material_name, '')
      AND (
              a.recorded_at <  b.recorded_at
           OR (a.recorded_at = b.recorded_at AND a.byproduct_id < b.byproduct_id)
      -- 092: never touch soft-deleted rows — this dedupe re-runs on every
      -- deploy (scripts/migrate.py has no applied-migrations ledger), and
      -- without this it would hard-purge the soft-delete audit trail that
      -- the Accounting CRUD DELETE endpoint writes.
      AND a.deleted_at IS NULL
      AND b.deleted_at IS NULL
          );

    -- 2. Drop the stale single-key UNIQUE (job_card_id, category) from 018,
    --    whatever its auto-generated name (mirrors 034's drop logic).
    FOR c IN
        SELECT con.conname
        FROM   pg_constraint con
        JOIN   pg_class      rel ON rel.oid = con.conrelid
        WHERE  rel.relname = 'job_card_byproducts_v2'
          AND  con.contype = 'u'
          AND  ARRAY(
                 SELECT attname::text FROM pg_attribute
                 WHERE  attrelid = rel.oid AND attnum = ANY(con.conkey)
                 ORDER  BY array_position(con.conkey, attnum)
               ) = ARRAY['job_card_id','category']::text[]
    LOOP
        EXECUTE format('ALTER TABLE job_card_byproducts_v2 DROP CONSTRAINT %I', c.conname);
    END LOOP;

    -- 3. Create the expression unique index the ON CONFLICT relies on.
    CREATE UNIQUE INDEX IF NOT EXISTS uq_byproducts_jc_cat_mat
        ON job_card_byproducts_v2 (job_card_id, category, COALESCE(material_name, ''));
END $$;

COMMIT;

-- ── Verification ─────────────────────────────────────────────────────────
-- (1) index must now exist:
SELECT indexname
FROM   pg_indexes
WHERE  indexname = 'uq_byproducts_jc_cat_mat';

-- (2) zero duplicate groups must remain:
SELECT job_card_id, category, COALESCE(material_name, '') AS material, COUNT(*) AS dupes
FROM   job_card_byproducts_v2
GROUP  BY job_card_id, category, COALESCE(material_name, '')
HAVING COUNT(*) > 1;
