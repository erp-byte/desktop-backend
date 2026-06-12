-- 054_npd_review_hold.sql  (NPD review: approve / reject / HOLD with a reason)
-- The business head SENDS the requisition; the NPD team then reviews it —
--   approve -> BH_APPROVED (promotable), reject -> BH_REJECTED, hold -> ON_HOLD —
-- each with a reason. This adds the ON_HOLD requisition status and the HOLD
-- approval action to the two anonymous CHECK constraints (drop + re-add named,
-- mirroring 042's pattern). Idempotent.

ALTER TABLE sample_requisitions DROP CONSTRAINT IF EXISTS sample_requisitions_status_check;
ALTER TABLE sample_requisitions ADD CONSTRAINT sample_requisitions_status_check
    CHECK (status IN (
        'DRAFT','SUBMITTED','BH_APPROVED','BH_REJECTED','ON_HOLD',
        'IN_PRODUCTION','PACKING','READY_FOR_DISPATCH',
        'INTERNALLY_DISPATCHED','PARTIALLY_CONVERTED',
        'GATE_PASS_ISSUED','CLOSED','CANCELLED'
    ));

ALTER TABLE sample_approvals DROP CONSTRAINT IF EXISTS sample_approvals_action_check;
ALTER TABLE sample_approvals ADD CONSTRAINT sample_approvals_action_check
    CHECK (action IN ('PENDING','APPROVED','REJECTED','HOLD'));
