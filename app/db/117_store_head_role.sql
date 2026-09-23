-- ===========================================================================
-- 117_store_head_role.sql
-- The stores role, and every grant written for it.
--
-- 085 creates `store_head`, but the runner walks the files in order, so on a
-- database where 085 had not yet run, every grant written for the role BEFORE
-- it exists is a silent no-op: the INSERT ... SELECT simply matches no row.
-- That is the state of RDS today — the floor requisition permissions from 111
-- exist, the role does not, so only admins can issue a request. Re-running the
-- whole set would fix it a file at a time over two deploys; this file does it
-- in one, whatever order the earlier files ran in.
--
-- It grants NOTHING NEW: each block mirrors the file named above it. After
-- applying, assign the role to the store's users in Admin -> Users.
-- Idempotent; one transaction.
-- ===========================================================================

BEGIN;

-- 085: the role itself.
INSERT INTO auth_role (role_name, description, is_admin)
SELECT 'store_head', 'Stores', FALSE
 WHERE NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'store_head');

-- 005 (all four purchase.po actions) and 087 (purchase.po read).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'purchase' AND p.sub_module = 'po'
ON CONFLICT DO NOTHING;

-- 075 and 085: Material In (stores receiving).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'purchase' AND p.sub_module = 'material_in'
ON CONFLICT DO NOTHING;

-- 006: receipt documents (COA + invoice), read/create/update/delete.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'receipt'
ON CONFLICT DO NOTHING;

-- 007: NCR record read + approve (cancel / reopen oversight).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'ncr'
   AND p.sub_module = 'record'
   AND p.action IN ('read', 'approve')
ON CONFLICT DO NOTHING;

-- 111: floor requisitions — store issues, and may cancel one it cannot supply.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'production'
   AND p.sub_module = 'floor_requisitions'
   AND p.sub_sub_module IS NULL
   AND p.action IN ('view', 'issue', 'cancel')
ON CONFLICT DO NOTHING;

COMMIT;
