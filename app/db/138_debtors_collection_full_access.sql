-- 138_debtors_collection_full_access.sql - the debtors_collection role gets the
-- whole Debtors Collection module.
--
-- 136 gave the role view, call, edit and export and left upload (replacing a tab
-- from a Tally export) and delete (removing a call summary) with admins. On
-- 2 Nov 2026 the business chose to give collections staff those two as well, so
-- this grants the role every module-level debtors_collection permission.
--
-- The module only: the role stays is_admin = FALSE and gets nothing outside
-- debtors_collection. MUST follow 136, which makes the role and the permissions;
-- run earlier, this matches no row and grants nothing.
--
-- Checked by the web app's /api/collections Route Handlers, which read /me on every
-- request, so it takes effect without a deploy of either side. Idempotent.

INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r
  CROSS JOIN auth_permission p
 WHERE r.role_name = 'debtors_collection'
   AND p.module = 'debtors_collection'
   AND p.sub_module IS NULL
   AND p.sub_sub_module IS NULL
   AND NOT EXISTS (SELECT 1 FROM auth_role_permission x
                    WHERE x.role_id = r.role_id AND x.permission_id = p.permission_id);
