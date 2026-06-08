-- =========================================================================
-- Migration 044: swap consumption + byproducts UNIQUE indexes to the
-- batch-aware shape the application code actually targets.
--
-- WHY:
--   POST /api/v1/production/job-cards-v2/{id}/outputs is 500ing with:
--     asyncpg.exceptions.UniqueViolationError:
--       duplicate key value violates unique constraint
--       "uq_jcmc_v2_jc_material"
--     DETAIL: Key (job_card_id, material_sku_name)=(35202080, Dried
--             Prunes (Pitted)) already exists.
--
--   upsert_consumption_lines (job_card_v2.py) and save_byproducts
--   (jc_accounting_v2.py) both upserted with batch_id in the conflict
--   target since the multi-batch redesign:
--     ON CONFLICT (job_card_id, COALESCE(batch_id, 0), material_sku_name)
--     ON CONFLICT (job_card_id, COALESCE(batch_id, 0), category,
--                  (COALESCE(material_name, '')))
--
--   The batch-aware indexes were declared in 038_jc_batch_per_record.sql
--   — which was never wired into scripts/migrate.py. Then 039 and 040
--   recreated the OLD (job_card_id, material_sku_name) /
--   (job_card_id, category, material_name) indexes to repair an unrelated
--   500. Net result on prod: only the OLD indexes exist; the new ON
--   CONFLICT clauses can't match them as the target index, BUT the OLD
--   tighter index fires during INSERT (it has fewer columns, so any new
--   row that re-uses material in a different batch violates it before
--   ON CONFLICT can engage).
--
-- WHAT (idempotent, safe to re-run):
--   1. De-dupe consumption rows on the batch-aware key — keep newest
--      per (job_card_id, COALESCE(batch_id, 0), material_sku_name).
--   2. Drop uq_jcmc_v2_jc_material (old, 2-col).
--   3. Create uq_jcmc_v2_jc_batch_material (new, 3-col with batch_id).
--   4. Same swap for byproducts: drop uq_byproducts_jc_cat_mat,
--      create uq_byproducts_jc_batch_cat_mat with batch_id.
--   5. Same swap for balance materials: drop the table-level UNIQUE
--      and create uq_jcbm_v2_jc_batch_bom_type with batch_id.
--
--   Guarded with to_regclass so a fresh DB without these tables no-ops.
--   IF re-run after 039/040 (e.g. someone re-runs migrate.py end-to-end),
--   039/040 will recreate their old indexes (their CREATE has
--   IF NOT EXISTS but the indexes won't exist any more because this
--   migration dropped them). 044 then runs AGAIN and re-drops + recreates
--   the batch-aware ones. Net state stays correct each iteration.
-- =========================================================================

BEGIN;

-- ── 1. CONSUMPTION ────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.job_card_material_consumption_v2') IS NULL THEN
        RAISE NOTICE 'job_card_material_consumption_v2 absent — skipping consumption swap';
        RETURN;
    END IF;

    -- De-dupe by the batch-aware key — keep newest per
    -- (job_card_id, COALESCE(batch_id, 0), material_sku_name). Matches
    -- the ON CONFLICT DO UPDATE semantics in upsert_consumption_lines
    -- (overwrites, not sums).
    DELETE FROM job_card_material_consumption_v2 a
    USING job_card_material_consumption_v2 b
    WHERE a.job_card_id              = b.job_card_id
      AND COALESCE(a.batch_id, 0)    = COALESCE(b.batch_id, 0)
      AND a.material_sku_name        = b.material_sku_name
      AND (
              a.recorded_at <  b.recorded_at
           OR (a.recorded_at = b.recorded_at AND a.consumption_id < b.consumption_id)
          );

    DROP INDEX IF EXISTS uq_jcmc_v2_jc_material;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_jcmc_v2_jc_batch_material
        ON job_card_material_consumption_v2
           (job_card_id, COALESCE(batch_id, 0), material_sku_name);
END $$;

-- ── 2. BYPRODUCTS ─────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.job_card_byproducts_v2') IS NULL THEN
        RAISE NOTICE 'job_card_byproducts_v2 absent — skipping byproducts swap';
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'job_card_byproducts_v2' AND column_name = 'material_name'
    ) THEN
        RAISE NOTICE 'material_name column absent — run 034 first; skipping byproducts swap';
        RETURN;
    END IF;

    -- De-dupe on the batch-aware key (matches save_byproducts'
    -- overwrite-on-conflict semantics).
    DELETE FROM job_card_byproducts_v2 a
    USING job_card_byproducts_v2 b
    WHERE a.job_card_id                       = b.job_card_id
      AND COALESCE(a.batch_id, 0)             = COALESCE(b.batch_id, 0)
      AND a.category                          = b.category
      AND COALESCE(a.material_name, '')       = COALESCE(b.material_name, '')
      AND (
              a.recorded_at <  b.recorded_at
           OR (a.recorded_at = b.recorded_at AND a.byproduct_id < b.byproduct_id)
          );

    DROP INDEX IF EXISTS uq_byproducts_jc_cat_mat;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_byproducts_jc_batch_cat_mat
        ON job_card_byproducts_v2
           (job_card_id, COALESCE(batch_id, 0), category,
            (COALESCE(material_name, '')));
END $$;

-- ── 3. BALANCE MATERIALS ──────────────────────────────────────────────
DO $$
DECLARE
    cn TEXT;
BEGIN
    IF to_regclass('public.job_card_balance_material_v2') IS NULL THEN
        RAISE NOTICE 'job_card_balance_material_v2 absent — skipping balance swap';
        RETURN;
    END IF;

    -- Drop any auto-named table-level UNIQUE on this table — 027's
    -- original (job_card_id, bom_line_id, balance_type) lives under a
    -- generated name (job_card_balance_material_v2_*_key).
    FOR cn IN
        SELECT con.conname
        FROM   pg_constraint con
        JOIN   pg_class      rel ON rel.oid = con.conrelid
        WHERE  rel.relname = 'job_card_balance_material_v2'
          AND  con.contype = 'u'
    LOOP
        EXECUTE format(
            'ALTER TABLE job_card_balance_material_v2 DROP CONSTRAINT %I',
            cn
        );
    END LOOP;

    CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_bom_type
        ON job_card_balance_material_v2
           (job_card_id, COALESCE(batch_id, 0),
            COALESCE(bom_line_id, 0), balance_type);
END $$;

COMMIT;

-- ── Verification ─────────────────────────────────────────────────────
-- (1) the new indexes must now exist:
SELECT indexname
FROM   pg_indexes
WHERE  indexname IN (
           'uq_jcmc_v2_jc_batch_material',
           'uq_byproducts_jc_batch_cat_mat',
           'uq_jcbm_v2_jc_batch_bom_type'
       )
ORDER  BY indexname;

-- (2) the old indexes must be gone:
SELECT indexname
FROM   pg_indexes
WHERE  indexname IN (
           'uq_jcmc_v2_jc_material',
           'uq_byproducts_jc_cat_mat'
       );

-- (3) zero duplicate groups on the new keys:
SELECT job_card_id, COALESCE(batch_id, 0) AS batch, material_sku_name, COUNT(*)
FROM   job_card_material_consumption_v2
GROUP  BY job_card_id, COALESCE(batch_id, 0), material_sku_name
HAVING COUNT(*) > 1;
