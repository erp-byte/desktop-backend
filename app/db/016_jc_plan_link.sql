-- =========================================================================
-- Migration 016: job-card ↔ plan v2 link + multi-shift time log
--
-- Three concerns bundled because they all serve the new "plan approval
-- generates per-floor job cards" workflow:
--   1. production_plan_v2.plan_id becomes app-supplied (8-digit time-based)
--      so it follows the same pattern as so_fulfillment_v2.so_fulfillment_id
--      and the JCs that link back to it carry a stable, human-typeable id.
--   2. job_card detaches from production_order: gets nullable plan_id /
--      plan_line_id / plan_step_id FKs into the v2 tables, plus
--      input_kind / output_kind / uom columns for the stage chain semantics
--      (RM → SFG → WIP → FG).
--   3. job_card_shift_log captures per-shift segments so a single JC can
--      legitimately span multiple shifts and multiple days.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- -----------------------------------------------------------------------
-- 1. Plan v2: drop the BIGSERIAL default so the application supplies the
--    8-digit time-based id (same pattern as so_fulfillment_v2). Existing
--    rows keep their small IDs; new rows get IDs from new_short_time_id().
--    The sequence is left in place (idle) — no need to drop it.
-- -----------------------------------------------------------------------
ALTER TABLE production_plan_v2 ALTER COLUMN plan_id DROP DEFAULT;

-- -----------------------------------------------------------------------
-- 2. job_card: link to plan v2, detach from production_order
--    - prod_order_id becomes nullable (legacy v1 flow still works, new
--      flow leaves it NULL).
--    - plan_id / plan_line_id / plan_step_id are the new lineage.
--    - input_kind  ∈ {RM, SFG, WIP}  — material received by this stage.
--    - output_kind ∈ {SFG, WIP, FG}  — material leaving this stage.
--      First stage in a chain has input_kind='RM'; last has output_kind='FG'.
--    - uom captures the planning UoM for the stage's qty; the per-kind
--      allowed list (per the bill-of-units sheet) is enforced in app code.
-- -----------------------------------------------------------------------
ALTER TABLE job_card
    ALTER COLUMN prod_order_id DROP NOT NULL;

ALTER TABLE job_card
    ADD COLUMN IF NOT EXISTS plan_id      BIGINT
        REFERENCES production_plan_v2(plan_id)      ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS plan_line_id BIGINT
        REFERENCES production_plan_line_v2(plan_line_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS plan_step_id BIGINT
        REFERENCES production_plan_step_v2(step_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS input_kind   TEXT
        CHECK (input_kind  IN ('RM','SFG','WIP')),
    ADD COLUMN IF NOT EXISTS output_kind  TEXT
        CHECK (output_kind IN ('SFG','WIP','FG')),
    ADD COLUMN IF NOT EXISTS uom          TEXT;

CREATE INDEX IF NOT EXISTS idx_job_card_plan      ON job_card(plan_id)
    WHERE plan_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_job_card_plan_line ON job_card(plan_line_id)
    WHERE plan_line_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_job_card_floor     ON job_card(floor)
    WHERE floor IS NOT NULL;

COMMENT ON COLUMN job_card.input_kind  IS
    'Material kind received at this stage. First stage in the chain = RM.';
COMMENT ON COLUMN job_card.output_kind IS
    'Material kind produced by this stage. Last stage in the chain = FG.';

-- -----------------------------------------------------------------------
-- 3. Multi-shift time capture
--    A JC may run across several shifts and several days. The existing
--    (start_time, end_time, total_time_min) trio on job_card cannot
--    represent that. We add an append-only segments table; the running
--    total stays mirrored on job_card.total_time_min for cheap reads.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_shift_log (
    log_id          BIGSERIAL PRIMARY KEY,
    job_card_id     INT NOT NULL
                    REFERENCES job_card(job_card_id) ON DELETE CASCADE,
    shift           TEXT NOT NULL CHECK (shift IN ('A','B','C','general')),
    shift_date      DATE NOT NULL,
    start_at        TIMESTAMPTZ NOT NULL,
    end_at          TIMESTAMPTZ,
    paused_minutes  INT NOT NULL DEFAULT 0 CHECK (paused_minutes >= 0),
    operator_name   TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_at IS NULL OR end_at >= start_at)
);

CREATE INDEX IF NOT EXISTS idx_jcsl_jc   ON job_card_shift_log(job_card_id);
-- Partial index: lookup of "is there an open segment for this JC?" is
-- frequent (called before start_shift to enforce mutual exclusion).
CREATE INDEX IF NOT EXISTS idx_jcsl_open ON job_card_shift_log(job_card_id)
    WHERE end_at IS NULL;

COMMENT ON TABLE  job_card_shift_log IS
    'Append-only time segments per job card. Multiple rows = multi-shift / '
    'multi-day stage. An open segment is one where end_at IS NULL.';

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
-- All three should report success.

SELECT 'plan_id default'  AS check,
       (SELECT column_default FROM information_schema.columns
         WHERE table_name='production_plan_v2' AND column_name='plan_id') AS default_value;

SELECT column_name, data_type, is_nullable
FROM   information_schema.columns
WHERE  table_name='job_card'
  AND  column_name IN ('prod_order_id','plan_id','plan_line_id','plan_step_id',
                       'input_kind','output_kind','uom')
ORDER  BY column_name;

SELECT table_name FROM information_schema.tables
 WHERE table_name='job_card_shift_log';
