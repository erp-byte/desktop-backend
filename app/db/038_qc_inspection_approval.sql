-- =========================================================================
-- 038_qc_inspection_approval.sql
--
-- Records WHO approved (set the verdict on) an inward inspection and WHEN.
-- The verdict decision is the manager's approval/sign-off; these columns
-- surface it as a first-class "Approval" record on the inspection detail.
--
-- Set by verdict_service.set_verdict / override (manager-only). Idempotent.
-- =========================================================================

ALTER TABLE qc_inward_inspection ADD COLUMN IF NOT EXISTS approved_by      INT;
ALTER TABLE qc_inward_inspection ADD COLUMN IF NOT EXISTS approved_by_name TEXT;
ALTER TABLE qc_inward_inspection ADD COLUMN IF NOT EXISTS approved_at      TIMESTAMPTZ;
