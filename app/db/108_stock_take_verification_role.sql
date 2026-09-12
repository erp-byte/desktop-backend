-- End-of-day sign-off for console stock adjustments: a `verify` action and the
-- `stock_take_verification` role that holds it.
--
-- NO SCHEMA CHANGE. new_stock_entries already carries verified / verified_by /
-- verified_at, and that is where a verification is recorded. stocktake_transactions
-- is append-only (trg_stk_txn_no_update raises on UPDATE), so it could never have
-- held a mutable flag anyway; a transaction's verification state is READ from the
-- adjustment row it rolls into, joined on the natural key that
-- uq_nse_adjustment_day already enforces:
--     (IST day, UPPER(BTRIM(item_name)), warehouse, floor_name, stock_type)
-- One verification therefore covers every transaction posted against that
-- article and place on that day, which is exactly the end-of-day flow: people
-- adjust all day, one person signs the day off at the end.
--
-- WHAT CHANGES IN BEHAVIOUR (transactions_service.write_back_entry)
-- Adjustments were written PRE-VERIFIED, each poster stamping their own name --
-- every ADJUSTMENT row in the table today reads verified=TRUE, verified_by =
-- the person who posted it. That was deliberate when the console wrote
-- stocktake_entries, because an unverified row there landed in the floor
-- managers' queue (Stock_Take/backend_st/routes/items.ts getFloorSummaries).
-- The console now writes new_stock_entries, which that app never reads, so the
-- reason is gone -- and self-verification is not verification.
--
-- WHY A SEPARATE ROLE RATHER THAN REUSING stock_take
-- The point of a sign-off is that someone OTHER than the person posting does it.
-- Folding `verify` into the stock_take role would grant both halves to everyone
-- who can adjust stock, which is the control this is meant to add.
-- `stock_take_verification` also gets `view`: a verifier who cannot read the
-- ledger cannot check anything before signing it.
--
-- IDEMPOTENT VIA `NOT EXISTS`, NOT `ON CONFLICT` -- same reason as 102: two of
-- the four key columns are NULL and the UNIQUE is NULLS DISTINCT, so ON CONFLICT
-- would insert a duplicate on every deploy.

BEGIN;

-- ── The role ───────────────────────────────────────────────────────────
INSERT INTO auth_role (role_name, description, is_admin)
SELECT 'stock_take_verification',
       'Stock take verification — signs off the day''s console stock adjustments',
       FALSE
 WHERE NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'stock_take_verification');

-- ── The action ─────────────────────────────────────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT 'stock_take', NULL, NULL, 'verify',
       'Mark the day''s console stock adjustments as verified, stamping verified_by and verified_at'
 WHERE NOT EXISTS (
        SELECT 1 FROM auth_permission p
         WHERE p.module = 'stock_take'
           AND p.sub_module IS NULL
           AND p.sub_sub_module IS NULL
           AND p.action = 'verify');

-- ── Grants ─────────────────────────────────────────────────────────────
-- The verification role gets view + verify. It deliberately does NOT get
-- create: signing off your own adjustment is the thing this separates.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r
  CROSS JOIN auth_permission p
 WHERE p.module = 'stock_take'
   AND p.sub_module IS NULL
   AND p.sub_sub_module IS NULL
   AND (
        (r.role_name = 'stock_take_verification' AND p.action IN ('view', 'verify'))
        -- Admins bypass check_permission entirely (permission_service.py:27);
        -- the row exists so the admin permission matrix shows the action.
     OR (r.is_admin AND p.action = 'verify')
   )
   AND NOT EXISTS (SELECT 1 FROM auth_role_permission x
                    WHERE x.role_id = r.role_id AND x.permission_id = p.permission_id);

COMMIT;
