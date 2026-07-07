-- 059 — hold start date for sample requisitions.
-- When the NPD reviewer puts a request ON_HOLD, they record a reason (already
-- captured in sample_approvals.remarks) AND the date the hold takes effect.
-- Additive + idempotent; nullable (only set on hold).
ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS hold_start_date DATE;
