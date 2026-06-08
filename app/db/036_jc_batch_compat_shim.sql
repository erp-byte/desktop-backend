-- =========================================================================
-- Migration 036: Phase → Batch compatibility shim (ADDITIVE — no rename).
--
-- Stage 1 of the Batch redesign, production-safe variant.
--
-- The original Stage 1 plan renamed the table outright, which would have
-- broken old code the moment psql committed.  Instead, this migration:
--
--   1. Keeps job_card_phase_v2 (the real table) UNTOUCHED.
--   2. Creates job_card_batch_v2 as an updatable VIEW over the table
--      with renamed columns (phase_id → batch_id, phase_number →
--      batch_number, phase_date → batch_date).  PG treats simple
--      column-rename views as automatically updatable, so the new
--      service's INSERT / UPDATE / DELETE / SELECT all flow straight
--      through to the underlying table.  RETURNING * works as expected.
--   3. Adds a sibling `batch_id` column to each of the three child
--      tables that today carry `phase_id` (output, partial_dispatch,
--      shift_log).  A BEFORE INSERT / UPDATE trigger keeps the two
--      columns in lockstep: write either one, the other auto-fills.
--   4. Backfills the existing `phase_id` values into the new
--      `batch_id` columns so already-persisted rows are accessible
--      via the new name from day one.
--
-- Result: old code (writing phase_id / SELECT FROM job_card_phase_v2)
-- and new code (writing batch_id / SELECT FROM job_card_batch_v2)
-- coexist for as long as needed.  Zero downtime, no data divergence,
-- and the FK constraints + CHECK constraints + UNIQUE indexes from
-- migrations 029 / 030 still fire (they live on the underlying table).
--
-- Cleanup happens later in migration 037, once the new code has been
-- running cleanly in production for a comfortable window:
--   * Drop the triggers + function.
--   * Drop the legacy `phase_id` columns.
--   * Drop the view.
--   * Rename the underlying job_card_phase_v2 → job_card_batch_v2 +
--     phase_id → batch_id (etc.) for real.  By that point no code is
--     reading the old names anymore.
--
-- Run order:
--   1. psql ... -f app/db/036_jc_batch_compat_shim.sql   (THIS file)
--   2. Restart FastAPI to load the new service code (writes batch_id
--      via the view; old phase_id reads still work).
--   3. Verify with the Stage 1 test plan.
--   4. (Later) psql ... -f app/db/037_jc_batch_cleanup.sql
--
-- Idempotent.  Safe to re-run.
-- =========================================================================

BEGIN;

-- ── 1. View: job_card_batch_v2 over job_card_phase_v2 ────────────────
-- PG auto-detects this as an updatable view because the rules are
-- simple (single base table, no DISTINCT, no aggregates, no joins).
-- INSERT / UPDATE / DELETE / SELECT all work natively through the view.
CREATE OR REPLACE VIEW job_card_batch_v2 AS
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
    notes
FROM job_card_phase_v2;

COMMENT ON VIEW job_card_batch_v2 IS
    'Phase→Batch compat view (migration 036).  Underlying table is '
    'job_card_phase_v2.  Auto-updatable: new service code writes '
    'batch_id / batch_number / batch_date; old code keeps reading '
    'phase_id etc. directly.  Cleanup (drop view + real rename) lands '
    'in migration 037.';

-- ── 2. Add batch_id sibling columns on the three child tables ────────
-- Each FK targets the same underlying column (phase_id) the legacy
-- phase_id column already targets.  After cleanup the FK auto-points
-- at the renamed (batch_id) column — PG tracks FKs by OID, not name.
ALTER TABLE job_card_output_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);

ALTER TABLE job_card_partial_dispatch_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);

ALTER TABLE job_card_shift_log_v2
    ADD COLUMN IF NOT EXISTS batch_id BIGINT
        REFERENCES job_card_phase_v2(phase_id);

-- Mirror the legacy phase_id index pattern so queries against
-- batch_id stay fast during the compat window.
CREATE INDEX IF NOT EXISTS idx_jco_v2_batch
    ON job_card_output_v2(batch_id);
CREATE INDEX IF NOT EXISTS idx_jcpd_v2_batch
    ON job_card_partial_dispatch_v2(batch_id);
CREATE INDEX IF NOT EXISTS idx_jcsl_v2_batch
    ON job_card_shift_log_v2(batch_id);

-- ── 3. Backfill batch_id from existing phase_id values ───────────────
UPDATE job_card_output_v2
   SET batch_id = phase_id
 WHERE batch_id IS NULL AND phase_id IS NOT NULL;

UPDATE job_card_partial_dispatch_v2
   SET batch_id = phase_id
 WHERE batch_id IS NULL AND phase_id IS NOT NULL;

UPDATE job_card_shift_log_v2
   SET batch_id = phase_id
 WHERE batch_id IS NULL AND phase_id IS NOT NULL;

-- ── 4. Sync trigger: keep batch_id ↔ phase_id mirrored on writes ─────
-- One filled column → populate the other.  Both filled AND mismatched
-- → raise, so a typo can't silently corrupt the row.  Both NULL is
-- left alone (the legacy nullable behaviour).
CREATE OR REPLACE FUNCTION fn_sync_phase_batch_id()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.batch_id IS NOT NULL AND NEW.phase_id IS NULL THEN
        NEW.phase_id := NEW.batch_id;
    ELSIF NEW.phase_id IS NOT NULL AND NEW.batch_id IS NULL THEN
        NEW.batch_id := NEW.phase_id;
    ELSIF NEW.batch_id IS NOT NULL
       AND NEW.phase_id IS NOT NULL
       AND NEW.batch_id <> NEW.phase_id
    THEN
        RAISE EXCEPTION
            'batch_id (%) and phase_id (%) must match — caller passed both',
            NEW.batch_id, NEW.phase_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_phase_batch_id_output
    ON job_card_output_v2;
CREATE TRIGGER trg_sync_phase_batch_id_output
    BEFORE INSERT OR UPDATE ON job_card_output_v2
    FOR EACH ROW EXECUTE FUNCTION fn_sync_phase_batch_id();

DROP TRIGGER IF EXISTS trg_sync_phase_batch_id_dispatch
    ON job_card_partial_dispatch_v2;
CREATE TRIGGER trg_sync_phase_batch_id_dispatch
    BEFORE INSERT OR UPDATE ON job_card_partial_dispatch_v2
    FOR EACH ROW EXECUTE FUNCTION fn_sync_phase_batch_id();

DROP TRIGGER IF EXISTS trg_sync_phase_batch_id_shift
    ON job_card_shift_log_v2;
CREATE TRIGGER trg_sync_phase_batch_id_shift
    BEFORE INSERT OR UPDATE ON job_card_shift_log_v2
    FOR EACH ROW EXECUTE FUNCTION fn_sync_phase_batch_id();

COMMIT;
