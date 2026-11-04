-- ===========================================================================
-- 121_transfer_request_decisions.sql
-- Transfer requests: the store's answer and the inventory manager's decision.
--
-- A request moves Pending -> Accepted (the supplying store accepts) -> Approved
-- (an inventory manager approves) -> Transferred (the dispatch is created).
-- Rejected is terminal and is reached from Pending (the store rejects) or from
-- Accepted (the inventory manager rejects). There is no hold step.
--
-- `status` carries the lifecycle. The eight columns added here record each
-- side's decision, who made it and when. All are nullable, and NULL means that
-- side has not decided. reject_reason / rejected_ts are legacy: the backend
-- still returns them and never writes them.
--
-- interunit_transfer_requests is managed outside this repo, like the other
-- interunit_* tables, so the ALTER is guarded with to_regclass (as in 063): on
-- a database without the table only the permissions below are created.
--
-- Permissions, which the four /api/v1/transfer/requests/{id}/... decision
-- endpoints are gated on:
--   transfer / requests / accept    store_head
--   transfer / requests / approve   inventory_manager, inventory_manager_substitute
-- The catalog insert uses the NULL-safe NOT EXISTS guard from 084 / 111
-- (auth_permission's UNIQUE treats a NULL sub_sub_module as distinct, so
-- ON CONFLICT alone would duplicate rows). A role that does not exist is
-- granted nothing: the INSERT ... SELECT matches no row for it.
--
-- MUST run before the backend that reads these columns is deployed: every
-- request read (GET /api/v1/transfer/requests) selects them. MUST follow 117,
-- which creates store_head.
--
-- Numbered 121 because the frontend refers to backend migrations 119 and 120,
-- which are not in this repo.
-- Idempotent; one transaction.
-- ===========================================================================

BEGIN;

-- ── The columns ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.interunit_transfer_requests') IS NOT NULL THEN
        ALTER TABLE public.interunit_transfer_requests
            ADD COLUMN IF NOT EXISTS store_decision        varchar,
            ADD COLUMN IF NOT EXISTS store_decided_by      varchar,
            ADD COLUMN IF NOT EXISTS store_decided_at      timestamp without time zone,
            ADD COLUMN IF NOT EXISTS store_reject_reason   text,
            ADD COLUMN IF NOT EXISTS manager_decision      varchar,
            ADD COLUMN IF NOT EXISTS manager_decided_by    varchar,
            ADD COLUMN IF NOT EXISTS manager_decided_at    timestamp without time zone,
            ADD COLUMN IF NOT EXISTS manager_reject_reason text;

        COMMENT ON COLUMN public.interunit_transfer_requests.store_decision IS
            'The supplying store''s answer: Accepted or Rejected. NULL = the store has not answered.';
        COMMENT ON COLUMN public.interunit_transfer_requests.store_decided_by IS
            'Email of the store user who answered.';
        COMMENT ON COLUMN public.interunit_transfer_requests.store_decided_at IS
            'When the store answered.';
        COMMENT ON COLUMN public.interunit_transfer_requests.store_reject_reason IS
            'Why the store rejected the request. NULL unless store_decision is Rejected.';
        COMMENT ON COLUMN public.interunit_transfer_requests.manager_decision IS
            'The inventory manager''s decision on an accepted request: Approved or Rejected. NULL = not decided.';
        COMMENT ON COLUMN public.interunit_transfer_requests.manager_decided_by IS
            'Email of the inventory manager who decided.';
        COMMENT ON COLUMN public.interunit_transfer_requests.manager_decided_at IS
            'When the inventory manager decided.';
        COMMENT ON COLUMN public.interunit_transfer_requests.manager_reject_reason IS
            'Why the inventory manager rejected the request. NULL unless manager_decision is Rejected.';
    END IF;
END
$$;

-- ── Permission catalog ──────────────────────────────────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, v.sub_module, NULL, v.action, v.description
  FROM (VALUES
      ('transfer', 'requests', 'accept',  'Accept or reject a transfer request for the supplying store'),
      ('transfer', 'requests', 'approve', 'Approve or reject a transfer request the store has accepted')
  ) AS v(module, sub_module, action, description)
 WHERE NOT EXISTS (
     SELECT 1
       FROM auth_permission p
      WHERE p.module         = v.module
        AND p.sub_module     IS NOT DISTINCT FROM v.sub_module
        AND p.sub_sub_module IS NULL
        AND p.action         = v.action
 );

-- ── Grants ──────────────────────────────────────────────────────────────────
-- The store answers; an inventory manager (or the substitute) decides. Admins
-- bypass the permission check, so they need no row.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM (VALUES
      ('store_head', 'accept'),
      ('inventory_manager', 'approve'),
      ('inventory_manager_substitute', 'approve')
  ) AS g(role_name, action)
  JOIN auth_role r       ON r.role_name = g.role_name
  JOIN auth_permission p ON p.module = 'transfer'
                        AND p.sub_module = 'requests'
                        AND p.sub_sub_module IS NULL
                        AND p.action = g.action
ON CONFLICT DO NOTHING;

COMMIT;
