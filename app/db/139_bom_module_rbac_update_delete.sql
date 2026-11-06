-- 139_bom_module_rbac_update_delete.sql — catalog rows + admin grant for the BOM
-- module's two new write endpoints.
--
-- 095 seeded ('bom', NULL, NULL, 'view') and ('bom', NULL, NULL, 'create'). The
-- router in app/modules/bom/router.py now also gates:
--
--     PATCH  /api/v1/bom/{bom_id}   on ('bom', NULL, NULL, 'update')
--     DELETE /api/v1/bom/{bom_id}   on ('bom', NULL, NULL, 'delete')
--
-- DELETE is a SOFT delete (bom_header.is_active = FALSE); nothing is removed.
--
-- DEPLOY ORDER — same rule as 095: run this BEFORE the backend that depends on
-- it. permission_service.check_permission walks (module, sub, subsub, action) →
-- (module, sub, NULL, action) → (module, NULL, NULL, action) and with no catalog
-- row finds nothing at any level, so until this lands both endpoints 403 for
-- every non-admin (admins bypass the check in code).
--
-- IDEMPOTENT + NULL-SAFE for exactly the reason documented in 095: the
-- auth_permission UNIQUE is NULLS DISTINCT, both rows carry two NULLs, so the
-- NOT EXISTS + IS NOT DISTINCT FROM guard is what makes a re-run a no-op and the
-- ON CONFLICT is only a second line of defence.
--
-- ADMIN ONLY, as 095. No viewer / planner / production grants: the tuples exist
-- so they are grantable the day that changes. Opening the module up later means
-- a role grant here (or in a new file) AND the UI changes 095 lists.

-- ── 1. Catalog ───────────────────────────────────────────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, v.sub_module, v.sub_sub_module, v.action, v.description
  FROM (VALUES
      ('bom', NULL::text, NULL::text, 'update',
       'Edit a BOM header, lines or process route via PATCH /api/v1/bom/{bom_id}'),
      ('bom', NULL::text, NULL::text, 'delete',
       'Deactivate a BOM (soft delete: is_active = FALSE) via DELETE /api/v1/bom/{bom_id}')
  ) AS v(module, sub_module, sub_sub_module, action, description)
 WHERE NOT EXISTS (
     SELECT 1
       FROM auth_permission p
      WHERE p.module         = v.module
        AND p.sub_module     IS NOT DISTINCT FROM v.sub_module
        AND p.sub_sub_module IS NOT DISTINCT FROM v.sub_sub_module
        AND p.action         = v.action
 )
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

-- ── 2. Grants ────────────────────────────────────────────────────────────────
-- admin: parity with every other seed. Admins bypass the check in code
-- (middleware.py short-circuits on is_admin), but the rows keep the admin
-- Roles & Permissions tree honest. Restricted to the two actions this file
-- seeds so it cannot be mistaken for a blanket re-grant of 095's rows.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'admin'
  AND p.module = 'bom'
  AND p.sub_module IS NULL
  AND p.sub_sub_module IS NULL
  AND p.action IN ('update', 'delete')
ON CONFLICT DO NOTHING;
