-- ===========================================================================
-- 127_transfer_request_holds.sql
-- Transfer requests: the store and the inventory manager may hold a request.
--
--   store hold    by the supplying store on a Pending request. The request stays
--                 'Pending'; store_decision is 'Held', store_decided_by / _at say
--                 who and when, and the reason goes in store_hold_reason. The store
--                 can still accept or reject it.
--   manager hold  by an inventory manager on an Accepted request. Its status
--                 becomes 'On Hold'; manager_decision is 'Held', manager_decided_by
--                 / _at say who and when, and the reason goes in
--                 manager_hold_reason. An inventory manager can still approve or
--                 reject it.
--
-- A later accept / approve / reject overwrites the decision and its by / at, as
-- before; the two *_hold_reason columns stay as the hold's history. Both are
-- nullable; NULL means the request was never held at that step.
--
-- interunit_transfer_requests is managed outside this repo, so the ALTER is
-- guarded with to_regclass (as in 121).
--
-- Also widens transfer_request_wa_pending's action check (migration 126) to the
-- two holds: a Hold tap on WhatsApp asks for the reason as a Reject tap does.
-- Guarded the same way, and written so it holds whichever version of 126 ran.
--
-- MUST run BEFORE the backend that reads these columns is deployed: every
-- request read (GET /api/v1/transfer/requests) selects them. MUST follow 121
-- and 126. Idempotent; one transaction.
-- ===========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.interunit_transfer_requests') IS NOT NULL THEN
        ALTER TABLE public.interunit_transfer_requests
            ADD COLUMN IF NOT EXISTS store_hold_reason   text,
            ADD COLUMN IF NOT EXISTS manager_hold_reason text;

        COMMENT ON COLUMN public.interunit_transfer_requests.store_hold_reason IS
            'Why the supplying store put the request on hold (store_decision Held). Kept once the store answers.';
        COMMENT ON COLUMN public.interunit_transfer_requests.manager_hold_reason IS
            'Why the inventory manager put the request on hold (status On Hold). Kept once it is decided.';
        COMMENT ON COLUMN public.interunit_transfer_requests.store_decision IS
            'The supplying store''s answer: Accepted, Rejected or Held (still Pending). NULL = the store has not answered.';
        COMMENT ON COLUMN public.interunit_transfer_requests.manager_decision IS
            'The inventory manager''s decision on an accepted request: Approved, Rejected or Held (status On Hold). NULL = not decided.';
    END IF;

    IF to_regclass('transfer_request_wa_pending') IS NOT NULL THEN
        ALTER TABLE transfer_request_wa_pending
            DROP CONSTRAINT IF EXISTS transfer_request_wa_pending_action_check;
        ALTER TABLE transfer_request_wa_pending
            ADD CONSTRAINT transfer_request_wa_pending_action_check
            CHECK (action IN ('store_reject', 'reject', 'store_hold', 'hold'));
    END IF;
END
$$;

COMMIT;
