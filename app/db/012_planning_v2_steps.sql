-- =========================================================================
-- 012_planning_v2_steps.sql
-- Per-plan-line ordered process steps for manual planning. Each plan line
-- snapshots its BOM process route into rows here at creation time; the user
-- can then reorder, edit floor assignment, add or delete steps PER SO
-- without touching the master bom_process_route.
--
-- - Steps belong to a plan_line (CASCADE delete with the line)
-- - step_order is mutable; UNIQUE(plan_line_id, step_order) ensures
--   consistent ordering. Reorder ops should defer the constraint or use a
--   two-pass swap (set to negative ints, then to final positive ints).
-- - No machine references at this layer — only `floor` (free-text).
--
-- Idempotent. Safe to re-run. PostgreSQL ≥ 12.
-- =========================================================================

CREATE TABLE IF NOT EXISTS production_plan_step_v2 (
    step_id           BIGSERIAL PRIMARY KEY,
    plan_line_id      BIGINT NOT NULL
                      REFERENCES production_plan_line_v2(plan_line_id)
                      ON DELETE CASCADE,
    step_order        SMALLINT NOT NULL CHECK (step_order > 0),
    process_name      TEXT NOT NULL,
    stage             TEXT,
    floor             VARCHAR(30),
    std_time_min      NUMERIC(8,2) CHECK (std_time_min IS NULL OR std_time_min >= 0),
    loss_pct          NUMERIC(5,3) CHECK (loss_pct IS NULL OR loss_pct >= 0),
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pps_v2_line_order UNIQUE (plan_line_id, step_order)
        DEFERRABLE INITIALLY IMMEDIATE
);

CREATE INDEX IF NOT EXISTS idx_pps_v2_line
    ON production_plan_step_v2(plan_line_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_pps_v2_touch'
    ) THEN
        CREATE TRIGGER trg_pps_v2_touch
            BEFORE UPDATE ON production_plan_step_v2
            FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
    END IF;
END$$;

COMMENT ON TABLE  production_plan_step_v2 IS
    'v2 per-plan-line ordered process steps. Initial rows are snapshotted '
    'from bom_process_route at plan creation; subsequent edits (reorder, '
    'floor change, add, delete) are scoped to this plan line only and do '
    'NOT affect the master BOM.';
COMMENT ON COLUMN production_plan_step_v2.step_order IS
    'Position within the plan line, 1-based. Unique per plan_line_id. '
    'Constraint is DEFERRABLE so atomic reorders can pass through invalid '
    'intermediate states inside a transaction.';
COMMENT ON COLUMN production_plan_step_v2.floor IS
    'Free-text floor name. No FK; no constraint. Frontend supplies the '
    'value via plain text input.';
