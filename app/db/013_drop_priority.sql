-- =========================================================================
-- 013_drop_priority.sql
-- Remove the `priority` column from v2 fulfillment and plan-line tables.
-- The application no longer uses priority — order is driven by deadline
-- and (for steps) step_order. Dropping the column also drops any partial
-- index that referenced it (idx_sof_v2_open_priority).
--
-- Idempotent. Safe to re-run.
-- =========================================================================

-- Drop the partial index explicitly first. (DROP COLUMN cascades, but doing
-- this up-front keeps the dependency obvious.)
DROP INDEX IF EXISTS idx_sof_v2_open_priority;

ALTER TABLE so_fulfillment_v2
    DROP COLUMN IF EXISTS priority;

ALTER TABLE production_plan_line_v2
    DROP COLUMN IF EXISTS priority;
