-- =========================================================================
-- Migration 038: Stage 2 — push batch_id onto every accounting row.
--
-- Builds on the additive shim in 036.  No table renames; the
-- job_card_batch_v2 VIEW from 036 is dropped + recreated to include
-- the new per-batch summary columns added here.
--
-- Changes:
--
--   1. Add per-batch summary fields to job_card_phase_v2 (the underlying
--      table behind the batch view): input_qty_kg, fg_actual_kg,
--      fg_actual_units, process_loss_kg, control_sample_kg,
--      is_balanced, balance_difference_qty, closure_remarks,
--      opened_at_ist, closed_at_ist, ended_at_ist.
--
--   2. Add batch_id (BIGINT, NULLable, FK → phase_v2.phase_id) to the
--      four accounting tables that today are JC-scoped:
--        * job_card_material_consumption_v2
--        * job_card_byproducts_v2
--        * job_card_balance_material_v2
--        * job_card_additive_consumption_v2
--      Each gets an index.
--
--   3. Backfill batch_id from each JC's earliest existing batch row.
--      Rows belonging to JCs with no batch row are left NULL — the
--      service layer treats those as "legacy / pre-batch" and surfaces
--      them under a pseudo-batch in the UI roll-up.
--
--   4. Rebuild the UNIQUE constraints on those tables to include
--      batch_id so the same article can be recorded once per batch
--      instead of once per JC.  NULL batch_id collapses to 0 via
--      COALESCE so legacy rows still satisfy uniqueness.
--
--   5. Drop the "one open batch per JC" partial unique index
--      (uq_jcphase_one_open / uq_jcbatch_one_open) — Stage 2 allows
--      multiple open batches per the operator-approved design.
--
--   6. Drop the (job_card_id, phase_date) UNIQUE — multiple batches
--      per day are allowed.
--
--   7. Recreate the job_card_batch_v2 view with the new columns
--      surfaced via the same column-rename mapping.
--
-- Run order:
--   1. (Assumes 036 is already applied.)
--   2. psql ... -f app/db/038_jc_batch_per_record.sql
--   3. Restart FastAPI to load the new service code.
--   4. Run the Stage 2 manual test plan.
--
-- Idempotent.  Safe to re-run.
-- =========================================================================

BEGIN;

-- ── 1. Per-batch summary columns on the underlying phase table ─────
ALTER TABLE job_card_phase_v2
    ADD COLUMN IF NOT EXISTS input_qty_kg           NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS fg_actual_kg           NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS fg_actual_units        NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS process_loss_kg        NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS control_sample_kg      NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS is_balanced            BOOLEAN,
    ADD COLUMN IF NOT EXISTS balance_difference_qty NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS closure_remarks        TEXT,
    -- Human-readable IST literals stamped at open / close time.  Stored
    -- as TEXT (not TIMESTAMPTZ) so even a raw psql query without
    -- timezone setup shows the IST clock face the operator saw.
    ADD COLUMN IF NOT EXISTS opened_at_ist          TEXT,
    ADD COLUMN IF NOT EXISTS closed_at_ist          TEXT,
    ADD COLUMN IF NOT EXISTS ended_at_ist           TEXT;

-- ── 2. batch_id columns on the four accounting tables ─────────────
ALTER TABLE job_card_material_consumption_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);
ALTER TABLE job_card_byproducts_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);
ALTER TABLE job_card_balance_material_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);
ALTER TABLE job_card_additive_consumption_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);

CREATE INDEX IF NOT EXISTS idx_jcmc_v2_batch
    ON job_card_material_consumption_v2(batch_id);
CREATE INDEX IF NOT EXISTS idx_jcbp_v2_batch
    ON job_card_byproducts_v2(batch_id);
CREATE INDEX IF NOT EXISTS idx_jcbm_v2_batch
    ON job_card_balance_material_v2(batch_id);
CREATE INDEX IF NOT EXISTS idx_jcac_v2_batch
    ON job_card_additive_consumption_v2(batch_id);

-- ── 3. Backfill batch_id from each JC's earliest existing batch ───
-- We use MIN(phase_id) because IDs are monotonically increasing
-- (new_short_time_id is epoch-derived); the smallest ID for a JC
-- is the chronologically-first batch.  Rows whose JC has no batch
-- row remain NULL.

UPDATE job_card_material_consumption_v2 mc
   SET batch_id = (
       SELECT MIN(p.phase_id) FROM job_card_phase_v2 p
        WHERE p.job_card_id = mc.job_card_id
   )
 WHERE mc.batch_id IS NULL;

UPDATE job_card_byproducts_v2 bp
   SET batch_id = (
       SELECT MIN(p.phase_id) FROM job_card_phase_v2 p
        WHERE p.job_card_id = bp.job_card_id
   )
 WHERE bp.batch_id IS NULL;

UPDATE job_card_balance_material_v2 bm
   SET batch_id = (
       SELECT MIN(p.phase_id) FROM job_card_phase_v2 p
        WHERE p.job_card_id = bm.job_card_id
   )
 WHERE bm.batch_id IS NULL;

UPDATE job_card_additive_consumption_v2 ac
   SET batch_id = (
       SELECT MIN(p.phase_id) FROM job_card_phase_v2 p
        WHERE p.job_card_id = ac.job_card_id
   )
 WHERE ac.batch_id IS NULL;

-- ── 4. Rebuild UNIQUE constraints to include batch_id ─────────────
--
-- Existing legacy uniqueness:
--   consumption: (job_card_id, material_sku_name)
--   byproducts:  (job_card_id, category, COALESCE(material_name, ''))
--   balance:     (job_card_id, bom_line_id, balance_type)
--
-- New per-batch uniqueness (NULL batch_id collapses to 0 so legacy
-- rows stay unique within their JC):
--
-- Consumption: drop the old UNIQUE INDEX, create the new one keyed on
-- batch_id too.

DROP INDEX IF EXISTS uq_jcmc_v2_jc_material;
CREATE UNIQUE INDEX IF NOT EXISTS uq_jcmc_v2_jc_batch_material
    ON job_card_material_consumption_v2
       (job_card_id, COALESCE(batch_id, 0), material_sku_name);

-- Byproducts: 034 used an expression UNIQUE INDEX
-- (uq_byproducts_jc_cat_mat).  Drop and recreate keyed on batch_id.
DROP INDEX IF EXISTS uq_byproducts_jc_cat_mat;
CREATE UNIQUE INDEX IF NOT EXISTS uq_byproducts_jc_batch_cat_mat
    ON job_card_byproducts_v2
       (job_card_id, COALESCE(batch_id, 0), category,
        COALESCE(material_name, ''));

-- Balance: 027 used a table-level UNIQUE constraint, so we drop it by
-- locating the auto-generated name in pg_constraint and dropping by
-- name.  Then we recreate as a UNIQUE INDEX (constraints can't carry
-- COALESCE expressions in PG).
DO $$
DECLARE
    cn TEXT;
BEGIN
    SELECT con.conname INTO cn
    FROM pg_constraint con
    JOIN pg_class cls ON cls.oid = con.conrelid
    WHERE cls.relname = 'job_card_balance_material_v2'
      AND con.contype = 'u';
    IF cn IS NOT NULL THEN
        EXECUTE 'ALTER TABLE job_card_balance_material_v2 DROP CONSTRAINT '
                || quote_ident(cn);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_bom_type
    ON job_card_balance_material_v2
       (job_card_id, COALESCE(batch_id, 0),
        COALESCE(bom_line_id, 0), balance_type);

-- Additives: no UNIQUE today (only PK), nothing to drop.  We don't
-- add one — the operator may legitimately record the same additive
-- multiple times in a single batch with different remarks.

-- ── 5. Drop the "one open batch per JC" partial UNIQUE index ──────
-- Stage 2 allows multiple open batches simultaneously.
DROP INDEX IF EXISTS uq_jcphase_one_open;
DROP INDEX IF EXISTS uq_jcbatch_one_open;

-- ── 6. Drop UNIQUE (job_card_id, phase_date) — multi-per-day OK ───
DO $$
DECLARE
    cn TEXT;
BEGIN
    -- Locate by the constraint key columns (job_card_id, phase_date).
    SELECT con.conname INTO cn
    FROM pg_constraint con
    JOIN pg_class cls ON cls.oid = con.conrelid
    JOIN pg_attribute att1 ON att1.attrelid = cls.oid AND att1.attname = 'job_card_id'
    JOIN pg_attribute att2 ON att2.attrelid = cls.oid AND att2.attname = 'phase_date'
    WHERE cls.relname = 'job_card_phase_v2'
      AND con.contype = 'u'
      AND con.conkey @> ARRAY[att1.attnum::SMALLINT, att2.attnum::SMALLINT]
      AND array_length(con.conkey, 1) = 2;
    IF cn IS NOT NULL THEN
        EXECUTE 'ALTER TABLE job_card_phase_v2 DROP CONSTRAINT '
                || quote_ident(cn);
    END IF;
END $$;

-- ── 7. Recreate the job_card_batch_v2 view with new columns ────────
DROP VIEW IF EXISTS job_card_batch_v2;
CREATE VIEW job_card_batch_v2 AS
SELECT
    phase_id            AS batch_id,
    job_card_id,
    phase_number        AS batch_number,
    phase_date          AS batch_date,
    planned_qty_kg,
    produced_qty_kg,
    extra_give_away_qty,
    started_at,
    ended_at,
    status,
    closed_by,
    closed_at,
    notes,
    -- New per-batch summary columns added by this migration.
    input_qty_kg,
    fg_actual_kg,
    fg_actual_units,
    process_loss_kg,
    control_sample_kg,
    is_balanced,
    balance_difference_qty,
    closure_remarks,
    opened_at_ist,
    closed_at_ist,
    ended_at_ist
FROM job_card_phase_v2;

COMMENT ON VIEW job_card_batch_v2 IS
    'Phase→Batch compat view (migration 036, extended in 038 with '
    'per-batch summary + IST timestamps).  Auto-updatable.  Replaced '
    'by a real renamed table in migration 037 once the new code is '
    'live cleanly.';

COMMIT;
