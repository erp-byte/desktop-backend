-- =========================================================================
-- Migration 049: job_card_accounting_v2 → per-batch summary rows.
--
-- Why
-- ---
-- The Output & Accounting tab shows operators a per-batch view: each
-- closed batch has its own IS_BALANCED chip, kg breakdown, and loss
-- percentages. But job_card_accounting_v2 was created with a
-- job_card_id UNIQUE constraint (migration 018), so every
-- PUT /accounting/summary call overwrites the single JC-level row
-- with whatever batch's data the operator was looking at last.
--
-- The R9 close gate (close_job_card in job_card_v2.py) reads
-- is_balanced off that one row. With per-batch UIs writing into it,
-- the close gate effectively trusts "the last save's batch" and not
-- the JC-wide aggregate. Closing a JC with three balanced batches
-- can fail if the most recent save happened to write an unbalanced
-- batch's numbers; closing a JC with one unbalanced batch can
-- spuriously pass if the most recent save was a balanced batch.
--
-- Symptom on the screenshot the operator shared: Batch 3 shows
-- Process Loss 0.00 kg but Process Loss 6.00% — the percentages
-- come from job_card_accounting_v2 (last saved as ~0.18 kg) while
-- the kg display recomputes from the current per-batch detail
-- rows (0 kg). The single accounting row can't be both batch's
-- truth simultaneously.
--
-- What this migration does
-- ------------------------
--   1. Add batch_id BIGINT (NULLable) to job_card_accounting_v2,
--      FK → job_card_phase_v2(phase_id) — same column the four
--      other accounting tables got in migration 038.
--   2. Backfill batch_id on existing rows from each JC's earliest
--      phase row (same backfill pattern as 038).
--   3. Drop the job_card_id-only UNIQUE constraint inherited from
--      018.
--   4. Add a per-batch UNIQUE: (job_card_id, COALESCE(batch_id, 0)).
--      NULL batch_id collapses to 0 so legacy rows from pre-batch
--      JCs (rare; mostly already backfilled) still satisfy
--      uniqueness within their JC.
--   5. Replace the unbalanced filter index with one that includes
--      batch_id so close-gate queries don't full-scan.
--   6. Add idx on batch_id for cheap per-batch reads.
--
-- Service-layer changes that go with this migration
-- -------------------------------------------------
--   * save_accounting accepts batch_id and writes into
--     ON CONFLICT (job_card_id, COALESCE(batch_id, 0)).
--   * router PUT /accounting/summary accepts batch_id in the body.
--   * close_job_card's R9 gate aggregates across all batches:
--     a JC is closeable when EVERY existing accounting row
--     (across batches) has is_balanced = true (and at least one
--     row exists).
--   * get_job_card returns the JC-wide accounting roll-up plus a
--     per-batch breakdown so the SummaryCard can render both.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- ── 1. batch_id column on the accounting table ──────────────────────────
ALTER TABLE job_card_accounting_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);

CREATE INDEX IF NOT EXISTS idx_jca_v2_batch
    ON job_card_accounting_v2(batch_id);

-- ── 2. Backfill batch_id from each JC's earliest existing batch ────────
-- Matches migration 038's strategy: MIN(phase_id) per JC (monotonically
-- increasing time-derived id ⇒ chronologically first batch).
UPDATE job_card_accounting_v2 acc
   SET batch_id = (
       SELECT MIN(p.phase_id) FROM job_card_phase_v2 p
        WHERE p.job_card_id = acc.job_card_id
   )
 WHERE acc.batch_id IS NULL;

-- ── 3. Drop the JC-level UNIQUE ────────────────────────────────────────
-- 018 declared `job_card_id BIGINT NOT NULL UNIQUE` at the column level
-- without naming the constraint, so PG auto-named it
-- `job_card_accounting_v2_job_card_id_key`. Look it up in pg_constraint
-- (defensive against PG version differences) and drop by name.
DO $$
DECLARE
    cn TEXT;
BEGIN
    SELECT con.conname INTO cn
    FROM pg_constraint con
    JOIN pg_class cls ON cls.oid = con.conrelid
    WHERE cls.relname = 'job_card_accounting_v2'
      AND con.contype = 'u'
      AND array_length(con.conkey, 1) = 1
      AND (
        SELECT a.attname FROM pg_attribute a
        WHERE a.attrelid = cls.oid AND a.attnum = con.conkey[1]
      ) = 'job_card_id';
    IF cn IS NOT NULL THEN
        EXECUTE 'ALTER TABLE job_card_accounting_v2 DROP CONSTRAINT '
                || quote_ident(cn);
    END IF;
END $$;

-- ── 4. Per-batch UNIQUE (allows one accounting row per (JC, batch)) ────
-- COALESCE(batch_id, 0) — pre-batch JCs that never had a batch row keep
-- a single row keyed at "batch 0" (sentinel) so legacy callers without
-- a batch context still get the expected one-row-per-JC behaviour.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jca_v2_jc_batch
    ON job_card_accounting_v2 (job_card_id, COALESCE(batch_id, 0));

-- ── 5. Replace the unbalanced filter index with a batch-aware one ─────
DROP INDEX IF EXISTS idx_jca_v2_unbalanced;
CREATE INDEX IF NOT EXISTS idx_jca_v2_jc_batch_unbalanced
    ON job_card_accounting_v2 (job_card_id, batch_id)
    WHERE is_balanced = FALSE;

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
SELECT column_name, data_type, is_nullable
FROM   information_schema.columns
WHERE  table_name = 'job_card_accounting_v2'
  AND  column_name IN ('job_card_id', 'batch_id')
ORDER  BY column_name;

SELECT indexname
FROM   pg_indexes
WHERE  tablename = 'job_card_accounting_v2'
ORDER  BY indexname;
