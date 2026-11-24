-- ===========================================================================
-- 142_stock_take_floor_checks.sql
-- Stock Take: a floor's daily "checked - no movement" report, and the
-- inventory manager's approval of it.
--
-- Asked for on 23 Nov 2026. Management wants a stock take on every floor every
-- day, but a floor where nothing moved has nothing to post, so its last entry
-- date goes stale on every report. An adjustment of 0 is not the answer: the
-- ledger refuses it (units > 0 and qty_kg > 0) and an adjustment means stock
-- moved. Instead the floor reports "checked, nothing moved" once for the day,
-- and an inventory manager approves or rejects that report.
--
-- One row per floor per IST day:
--   pending   the floor has reported; waiting for an inventory manager
--   approved  an inventory manager agreed, in the ERP or from WhatsApp
--   rejected  an inventory manager did not; the floor may report again, which
--             puts the same row back to pending (submissions counts the times)
--
-- No stock figure is read from or written to here. new_stock_entries and
-- stocktake_transactions are untouched.
--
-- warehouse is the code as Stock Take keys it: upper case, no hyphen ('W202',
-- 'A185'). floor_name is the floor as FLOORS_BY_WAREHOUSE spells it.
--
-- Permissions:
--   stock_take / approve   NEW. Approve or reject a floor's check.
--                          inventory_manager and admin.
--   stock_take / view      also granted to inventory_manager, which held no
--                          Stock Take permission at all: an approver must be
--                          able to open the list of checks they decide on.
-- Reporting a check needs stock_take / create, the right to post adjustments,
-- which the stock_take role already holds. Nothing changes for it.
--
-- The catalog insert uses the NULL-safe NOT EXISTS guard from 108 (the UNIQUE
-- treats NULL sub_module as distinct, so ON CONFLICT alone would duplicate rows).
-- A role that does not exist is granted nothing.
--
-- Run BEFORE the backend that reads this table is deployed. Until then the
-- backend answers the floor-check routes with "not installed" and every other
-- Stock Take route works as before.
-- Idempotent; one transaction.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.stocktake_floor_checks (
    check_id            bigserial    PRIMARY KEY,
    check_day           date         NOT NULL,
    warehouse           text         NOT NULL,
    floor_name          text         NOT NULL,
    status              text         NOT NULL DEFAULT 'pending',
    note                text,
    checked_by          text         NOT NULL,
    checked_by_user_id  integer,
    checked_at          timestamptz  NOT NULL DEFAULT now(),
    submissions         integer      NOT NULL DEFAULT 1,
    decided_by          text,
    decided_by_user_id  integer,
    decided_at          timestamptz,
    decided_via         text,
    reject_reason       text,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT chk_stfc_status      CHECK (status IN ('pending', 'approved', 'rejected')),
    CONSTRAINT chk_stfc_decided_via CHECK (decided_via IS NULL OR decided_via IN ('erp', 'whatsapp')),
    -- A decision is recorded exactly when there is one.
    CONSTRAINT chk_stfc_decision    CHECK ((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT chk_stfc_warehouse   CHECK (warehouse = UPPER(REPLACE(BTRIM(warehouse), '-', ''))),
    CONSTRAINT chk_stfc_submissions CHECK (submissions >= 1)
);

-- One report per floor per day, whatever the floor's spelling.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stfc_floor_day
    ON public.stocktake_floor_checks (check_day, warehouse, UPPER(BTRIM(floor_name)));

-- The approver's queue.
CREATE INDEX IF NOT EXISTS ix_stfc_pending
    ON public.stocktake_floor_checks (check_day DESC)
    WHERE status = 'pending';

COMMENT ON TABLE public.stocktake_floor_checks IS
    'A floor''s daily "checked - no movement" report and the inventory manager''s decision on it. One row per floor per IST day. No stock is read or changed here. Migration 142.';
COMMENT ON COLUMN public.stocktake_floor_checks.check_day IS
    'The IST day the floor reports on. Always the day it was reported.';
COMMENT ON COLUMN public.stocktake_floor_checks.warehouse IS
    'Warehouse code as Stock Take keys it: upper case, no hyphen (W202, A185).';
COMMENT ON COLUMN public.stocktake_floor_checks.submissions IS
    'How many times the floor has reported this day: 1, plus 1 each time it reports again after a rejection.';
COMMENT ON COLUMN public.stocktake_floor_checks.decided_via IS
    'Where the inventory manager decided: erp (the Stock Take screen) or whatsapp (a tap on the notice).';

-- ── Permission catalog ──────────────────────────────────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT 'stock_take', NULL, NULL, 'approve',
       'Approve or reject a floor''s daily "checked - no movement" report'
 WHERE NOT EXISTS (
        SELECT 1 FROM auth_permission p
         WHERE p.module = 'stock_take'
           AND p.sub_module IS NULL
           AND p.sub_sub_module IS NULL
           AND p.action = 'approve');

-- ── Grants ──────────────────────────────────────────────────────────────────
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r
  CROSS JOIN auth_permission p
 WHERE p.module = 'stock_take'
   AND p.sub_module IS NULL
   AND p.sub_sub_module IS NULL
   AND (
        (r.role_name = 'inventory_manager' AND p.action IN ('view', 'approve'))
        -- Admins bypass check_permission; the row makes the admin matrix show it.
     OR (r.is_admin AND p.action = 'approve')
   )
   AND NOT EXISTS (SELECT 1 FROM auth_role_permission x
                    WHERE x.role_id = r.role_id AND x.permission_id = p.permission_id);

COMMIT;
