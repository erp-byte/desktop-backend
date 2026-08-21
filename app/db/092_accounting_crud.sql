-- 092_accounting_crud — soft-delete columns + one-record-per-batch keys for the
-- Accounting CRUD endpoints (GET/POST/PUT/DELETE /api/v1/production/accounting/record).
--
-- WHY THIS EXISTS
-- The Accounting tab currently writes through a composite POST /outputs that
-- APPENDS a new job_card_output_v2 row on every save. That is a history model,
-- not a record model: production carries 1,507 output rows across only 562
-- distinct (job_card_id, batch_id) pairs — 453 duplicate groups, worst case 17
-- rows on a single batch. CRUD needs exactly one live record per batch, so this
-- migration collapses the duplicates (soft, never destructive) and pins the
-- invariant with a partial unique index.
--
-- WHAT IT DOES
--   1. deleted_at / deleted_by on the six tables the accounting payload writes.
--   2. batch_id on job_card_qc_v2 so the qc block is per-batch like every other
--      section (table is EMPTY — 0 rows — so there is nothing to backfill).
--   3. Supersedes older job_card_output_v2 rows, keeping the newest per batch.
--   4. Partial unique indexes (WHERE deleted_at IS NULL) enforcing one live row
--      per batch on outputs, and one live row per additive per batch.
--   5. Partial indexes to keep the "live rows only" reads cheap.
--
-- ⚠ WHAT IT DELIBERATELY DOES *NOT* DO
-- It does NOT convert the EXISTING unique indexes on
-- job_card_material_consumption_v2, job_card_byproducts_v2 or
-- job_card_balance_material_v2 into partial (WHERE deleted_at IS NULL) indexes.
-- Those exact index expressions are named as ON CONFLICT targets by
-- services/jc_accounting_v2.py (save_consumption :616, save_byproducts :793) and
-- replace_balance_materials. Postgres only matches an ON CONFLICT inference
-- clause to a PARTIAL index when the statement repeats the index predicate, so
-- making them partial would break those upserts instantly.
-- Consequence, and it is load-bearing for the CRUD service: a soft-deleted row
-- STILL OCCUPIES its unique key. Re-adding the same material after deleting it
-- therefore collides, and the insert path must RESURRECT the row by setting
-- deleted_at = NULL in its ON CONFLICT DO UPDATE. jc_accounting_crud.py does
-- exactly that — see _upsert_section().
--
-- ⚠ LOCKING: plain ALTER TABLE ... ADD COLUMN with no default and no rewrite is
-- a catalog-only change in PG 11+ (fast). CREATE INDEX takes a SHARE lock while
-- it builds; these tables are small (largest ~1.7k rows) so that is sub-second.
-- CREATE INDEX CONCURRENTLY is not usable here because scripts/migrate.py runs
-- each file as one multi-statement string inside an implicit transaction.
--
-- Idempotent — safe to re-run. Re-running after the backfill is a no-op because
-- the supersede UPDATE only touches rows that are still live AND not-newest.

-- ── 1. Soft-delete columns ───────────────────────────────────────────────────
ALTER TABLE job_card_output_v2               ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE job_card_output_v2               ADD COLUMN IF NOT EXISTS deleted_by TEXT;
ALTER TABLE job_card_material_consumption_v2 ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE job_card_material_consumption_v2 ADD COLUMN IF NOT EXISTS deleted_by TEXT;
ALTER TABLE job_card_byproducts_v2           ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE job_card_byproducts_v2           ADD COLUMN IF NOT EXISTS deleted_by TEXT;
ALTER TABLE job_card_balance_material_v2     ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE job_card_balance_material_v2     ADD COLUMN IF NOT EXISTS deleted_by TEXT;
ALTER TABLE job_card_additive_consumption_v2 ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE job_card_additive_consumption_v2 ADD COLUMN IF NOT EXISTS deleted_by TEXT;
ALTER TABLE job_card_qc_v2                   ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE job_card_qc_v2                   ADD COLUMN IF NOT EXISTS deleted_by TEXT;

-- ── 2. QC becomes per-batch ──────────────────────────────────────────────────
-- job_card_qc_v2 was JC-level (UNIQUE on job_card_id alone), which cannot
-- express "QC for batch 2" — a save against batch 2 would silently overwrite
-- batch 1's result. The table has 0 rows, so widening the key is free.
--
-- services/job_card_v2.py::upsert_qc names `ON CONFLICT (job_card_id)` and is
-- updated in the same commit to target the new key. Nothing else writes here.
ALTER TABLE job_card_qc_v2 ADD COLUMN IF NOT EXISTS batch_id BIGINT;

DO $$
BEGIN
    -- Drop the old JC-level uniqueness however it was expressed (027 created it
    -- as a constraint on some environments and a bare index on others).
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'job_card_qc_v2_job_card_id_key') THEN
        ALTER TABLE job_card_qc_v2 DROP CONSTRAINT job_card_qc_v2_job_card_id_key;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_indexes
                WHERE tablename = 'job_card_qc_v2'
                  AND indexname = 'job_card_qc_v2_job_card_id_key') THEN
        DROP INDEX job_card_qc_v2_job_card_id_key;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_jc_qc_v2_jc_batch
    ON job_card_qc_v2 (job_card_id, COALESCE(batch_id, 0));

-- ── 3. Collapse job_card_output_v2 to one live row per batch ─────────────────
-- "Newest" = highest recorded_at, ties broken by output_id so the choice is
-- deterministic (recorded_at has second-ish resolution and bulk saves can share
-- a timestamp; without the tiebreak, a re-run could pick a different survivor
-- and the index build would be non-reproducible).
--
-- NULLS LAST on recorded_at: a NULL timestamp is treated as OLDEST, so a row
-- that actually carries a date always wins over one that does not.
UPDATE job_card_output_v2 o
   SET deleted_at = NOW(),
       deleted_by = 'migration_092_supersede'
  FROM (
        SELECT output_id,
               ROW_NUMBER() OVER (
                   PARTITION BY job_card_id, COALESCE(batch_id, 0)
                   ORDER BY recorded_at DESC NULLS LAST, output_id DESC
               ) AS rn
          FROM job_card_output_v2
         WHERE deleted_at IS NULL
       ) ranked
 WHERE o.output_id = ranked.output_id
   AND ranked.rn > 1
   AND o.deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_jc_output_v2_live_per_batch
    ON job_card_output_v2 (job_card_id, COALESCE(batch_id, 0))
 WHERE deleted_at IS NULL;

-- ── 4. Additives gain a natural key ──────────────────────────────────────────
-- The table had only its additive_id PK, so per-line diffing had nothing to
-- match on. sku_name (dropdown) and material_name (free-text "Others") are
-- mutually exclusive per the table's own CHECK, so COALESCE gives one key.
-- Verified 0 duplicate groups before adding, on both prod and the mock.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jc_additive_v2_live
    ON job_card_additive_consumption_v2
       (job_card_id, COALESCE(batch_id, 0), COALESCE(sku_name, material_name))
 WHERE deleted_at IS NULL;

-- ── 5. Live-row read indexes ─────────────────────────────────────────────────
-- Every CRUD read filters deleted_at IS NULL and scopes by (job_card_id,
-- batch_id). Partial indexes keep those reads off the dead rows entirely.
CREATE INDEX IF NOT EXISTS idx_jc_consumption_v2_live
    ON job_card_material_consumption_v2 (job_card_id, batch_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_jc_byproducts_v2_live
    ON job_card_byproducts_v2 (job_card_id, batch_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_jc_balance_v2_live
    ON job_card_balance_material_v2 (job_card_id, batch_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_jc_additive_v2_live
    ON job_card_additive_consumption_v2 (job_card_id, batch_id) WHERE deleted_at IS NULL;
