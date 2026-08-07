-- 084_vendor_permissions.sql — Vendor Management RBAC catalog + purchase grants.
--
-- WHY: Vendor Management is the third Purchase sub-tile (PO Upload / Material In
-- / Vendor Management) and every endpoint in modules/vendor/router.py is gated by
-- require_permission("vendor", ...). The catalog, though, only ever carried three
-- vendor rows (master.extract / master.revert / master.view_history), so the other
-- 17 tuples the router asks for could not resolve for ANY non-admin role:
-- check_permission walks (vendor, sub, NULL, action) → (vendor, NULL, NULL, action),
-- finds no catalog row at either level, and denies. purchase_manager already holds
-- every purchase.* grant (PO Upload + Material In) and is the role the frontend
-- scopes to the Purchase tile (ROLE_MODULE_SCOPE, web_replica/src/lib/modules.tsx),
-- but it held ZERO vendor grants — so Vendor Management 403'd for it.
--
-- This migration (1) completes the vendor catalog and (2) grants the whole vendor
-- module to purchase_manager (+ admin, for parity with the other seeds; admins
-- bypass the check anyway).
--
-- IDEMPOTENT + NULL-SAFE: auth_permission's UNIQUE (module, sub_module,
-- sub_sub_module, action) treats NULLs as DISTINCT, so ON CONFLICT DO NOTHING does
-- NOT stop a duplicate here — every vendor row has sub_sub_module IS NULL. The
-- NOT EXISTS + IS NOT DISTINCT FROM guard below is what makes a re-run a no-op.
-- (This is the same NULLS-DISTINCT trap that duplicated the RDS catalog 208×.)

-- ── 1. Catalog: the 17 vendor tuples the router gates on but never existed ───
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, v.sub_module, NULL, v.action, v.description
  FROM (VALUES
      ('vendor', 'master',   'view',    'View vendor master records'),
      ('vendor', 'master',   'create',  'Onboard a new vendor'),
      ('vendor', 'master',   'update',  'Edit vendor master fields'),
      ('vendor', 'master',   'delete',  'Delete a vendor'),
      ('vendor', 'master',   'approve', 'Approve a vendor'),
      ('vendor', 'banking',  'view',    'View vendor bank accounts'),
      ('vendor', 'banking',  'create',  'Add a vendor bank account'),
      ('vendor', 'banking',  'update',  'Edit a vendor bank account'),
      ('vendor', 'banking',  'delete',  'Delete a vendor bank account'),
      ('vendor', 'document', 'view',    'View vendor documents'),
      ('vendor', 'document', 'create',  'Upload a vendor document'),
      ('vendor', 'document', 'update',  'Replace/edit a vendor document'),
      ('vendor', 'document', 'delete',  'Delete a vendor document'),
      ('vendor', 'contract', 'view',    'View vendor contracts'),
      ('vendor', 'contract', 'create',  'Add a vendor contract'),
      ('vendor', 'contract', 'update',  'Edit a vendor contract'),
      ('vendor', 'contract', 'delete',  'Delete a vendor contract')
  ) AS v(module, sub_module, action, description)
 WHERE NOT EXISTS (
     SELECT 1
       FROM auth_permission p
      WHERE p.module         = v.module
        AND p.sub_module     IS NOT DISTINCT FROM v.sub_module
        AND p.sub_sub_module IS NULL
        AND p.action         = v.action
 );

-- ── 2. Grants: purchase_manager gets the full Purchase tile ─────────────────
-- Vendor Management (this migration) + PO Upload & Material In (already granted
-- by auth_schema.sql's blanket `p.module = 'purchase'` clause; the re-assert is a
-- no-op that keeps this file self-sufficient if the catalog grew since).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name IN ('admin', 'purchase_manager')
   AND p.module IN ('vendor', 'purchase')
ON CONFLICT DO NOTHING;
