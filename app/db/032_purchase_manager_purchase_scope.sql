-- =========================================================================
-- 032_purchase_manager_purchase_scope.sql
--
-- Restricts the `purchase_manager` role to the Purchase umbrella ONLY:
-- the three backend modules that live under the Purchase tile in the UI —
--   purchase  (PO)
--   vendor    (vendor onboarding / master / banking / documents / contracts)
--   receipt   (Material-In / COA / invoice)
--
-- Removes the previously-seeded cross-module grants (production view-all,
-- production.indents/alerts create, and the broad ncr write/approve from
-- auth_schema.sql / 005_po_rebuild.sql / 007_ncr.sql).
--
-- Access is enforced purely by which auth_role_permission rows the role holds
-- (check_permission in permission_service.py). No per-module flag exists, so
-- "purchase only" == "only purchase/vendor/receipt permission rows".
--
-- Idempotent. Safe to re-run. Does NOT touch users, only the role's grants.
--
-- NOTE: this does not gate /api/v1/so/* (SO router is auth-only, no
-- require_permission) or the legacy /api/v1/purchase/* router (ungated).
-- =========================================================================

DO $$
DECLARE
  rid INT;
BEGIN
  SELECT role_id INTO rid FROM auth_role WHERE role_name = 'purchase_manager';
  IF rid IS NULL THEN
    RAISE NOTICE 'purchase_manager role not found — nothing to do';
    RETURN;
  END IF;

  -- 1) Revoke everything OUTSIDE the Purchase umbrella (production, ncr, …).
  DELETE FROM auth_role_permission rp
  USING auth_permission p
  WHERE rp.permission_id = p.permission_id
    AND rp.role_id = rid
    AND p.module NOT IN ('purchase', 'vendor', 'receipt');

  -- 2) Grant the FULL Purchase umbrella (purchase + vendor + receipt).
  INSERT INTO auth_role_permission (role_id, permission_id)
  SELECT rid, p.permission_id
    FROM auth_permission p
   WHERE p.module IN ('purchase', 'vendor', 'receipt')
  ON CONFLICT (role_id, permission_id) DO NOTHING;

  RAISE NOTICE 'purchase_manager scoped to purchase + vendor + receipt (role_id=%)', rid;
END $$;
