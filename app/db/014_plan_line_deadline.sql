-- =========================================================================
-- 014_plan_line_deadline.sql
-- Add per-line `deadline_date` to production_plan_line_v2.
--
-- Why nullable: not every line carries a customer-stated deadline
-- (internal SOs, RM ad-hoc plans, transfers). The frontend sends one when
-- known but schema must permit NULL.
--
-- Why NOT also adding `factory`: the plan header's `warehouse` column
-- already encodes the factory for the whole plan (we enforce one-factory-
-- per-plan client-side at submit time). A per-line factory column would
-- be redundant and could drift from the header.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

ALTER TABLE production_plan_line_v2
    ADD COLUMN IF NOT EXISTS deadline_date DATE;

COMMENT ON COLUMN production_plan_line_v2.deadline_date IS
    'Per-line target completion date. NULLABLE — not all SO lines carry an '
    'explicit deadline (internal SOs, RM ad-hoc plans). Independent of the '
    'plan header date_to (which is the plan window upper bound).';
