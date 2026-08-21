-- 086_requisition_bh_signoff.sql
-- Moves the BUSINESS-HEAD approval from the end of the NPD flow to its start.
--
-- Before: an NPD/TRIAL request went straight to the NPD team on submit, and the BH
--   only got asked anything at the very END — the dev job card's promote raised a
--   REQUESTOR_BH gate (074/075/079) alongside the inventory-manager one, so the BH
--   approved a recipe long after the work was done.
-- After: the BH approves the REQUEST, at the requisition stage, before NPD sees it.
--   The promote keeps only its INV_MGR gate (see promote_approval_service).
--
-- The gate is raised on submit ONLY when the request was raised by someone else:
--   sales_poc_user_id <> business_head_user_id. A BH raising their own request (the
--   two ids match, or no BH is named) is auto-approved with no message — they have
--   already said yes by raising it.
--
-- Additive + idempotent; existing rows keep bh_signoff_state NULL, which every read
-- path treats as "no gate" — in-flight requisitions are untouched.
-- Hand-apply via psql (migrate.py does not enumerate samples/):
--   psql "$DATABASE_URL" -f app/db/samples/086_requisition_bh_signoff.sql

-- 1. Gate state on the requisition -------------------------------------------------
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS bh_signoff_state TEXT,
    ADD COLUMN IF NOT EXISTS bh_signoff_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS bh_signoff_by    INTEGER REFERENCES auth_user(user_id);

ALTER TABLE sample_requisitions DROP CONSTRAINT IF EXISTS sample_requisitions_bh_signoff_state_check;
ALTER TABLE sample_requisitions ADD CONSTRAINT sample_requisitions_bh_signoff_state_check
    CHECK (bh_signoff_state IS NULL OR bh_signoff_state IN (
        'PENDING',          -- armed on submit; the BH has been messaged and NPD is held back
        'APPROVED',         -- the bound BH said yes -> the request is released to NPD
        'AUTO_APPROVED',    -- sales POC IS the BH (or no BH named) -> released, no message sent
        'REJECTED',         -- the bound BH said no -> requisition moved to BH_REJECTED
        'NOT_REQUIRED'      -- non-NPD flow, which keeps its own BH_APPROVAL step
    ));

COMMENT ON COLUMN sample_requisitions.bh_signoff_state IS
    'Requisition-stage business-head gate (086). NULL = pre-086 row, no gate. PENDING holds the request back from NPD.';
COMMENT ON COLUMN sample_requisitions.bh_signoff_at IS 'When the BH sign-off was decided (or auto-approved).';
COMMENT ON COLUMN sample_requisitions.bh_signoff_by IS 'Who decided the BH sign-off; the bound BH on an auto-approval.';

CREATE INDEX IF NOT EXISTS ix_sample_req_bh_signoff_pending
    ON sample_requisitions (bh_signoff_state) WHERE bh_signoff_state = 'PENDING';

-- 2. New approval stage on the audit chain ------------------------------------------
-- Its own stage rather than reusing BH_APPROVAL: for NPD/TRIAL the NPD team's review
-- already writes BH_APPROVAL rows (act_npd_review), so sharing the stage would make
-- "did the business head sign off?" unanswerable from the approvals table.
ALTER TABLE sample_approvals DROP CONSTRAINT IF EXISTS sample_approvals_approval_stage_check;
ALTER TABLE sample_approvals ADD CONSTRAINT sample_approvals_approval_stage_check
    CHECK (approval_stage IN (
        'BH_APPROVAL','REQUESTOR_BH_SIGNOFF','PRODUCTION_ACK','INV_MGR_VERIFICATION',
        'INV_MGR_SIGNOFF','CONVERSION_APPROVAL','CONVERSION_INV_MGR_SIGNOFF'
    ));

ALTER TABLE sample_approval_role_map DROP CONSTRAINT IF EXISTS sample_approval_role_map_approval_stage_check;
ALTER TABLE sample_approval_role_map ADD CONSTRAINT sample_approval_role_map_approval_stage_check
    CHECK (approval_stage IN (
        'BH_APPROVAL','REQUESTOR_BH_SIGNOFF','PRODUCTION_ACK','INV_MGR_VERIFICATION',
        'INV_MGR_SIGNOFF','CONVERSION_APPROVAL','CONVERSION_INV_MGR_SIGNOFF'
    ));

INSERT INTO sample_approval_role_map (approval_stage, sample_type, required_role) VALUES
    ('REQUESTOR_BH_SIGNOFF', '*', 'business_head')
ON CONFLICT (approval_stage, sample_type, entity, required_role) DO NOTHING;

-- 3. WhatsApp button mapping ---------------------------------------------------------
-- The BH's Approve/Reject tap quotes the template we sent; wa_review_message (067) is
-- already the requisition-scoped wamid map, so the gate rides it as a third kind rather
-- than getting a table of its own.
ALTER TABLE wa_review_message DROP CONSTRAINT IF EXISTS wa_review_message_kind_check;
ALTER TABLE wa_review_message ADD CONSTRAINT wa_review_message_kind_check
    CHECK (kind IN ('REVIEW', 'UPDATED', 'BH_SIGNOFF'));

-- A BH rejecting over WhatsApp must give a reason, captured on the next reply — the
-- same arm-and-wait the NPD hold flow uses, so wa_pending_action gains the action.
ALTER TABLE wa_pending_action DROP CONSTRAINT IF EXISTS wa_pending_action_action_check;
ALTER TABLE wa_pending_action ADD CONSTRAINT wa_pending_action_action_check
    CHECK (action IN ('HOLD', 'BH_REJECT'));
