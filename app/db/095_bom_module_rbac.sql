-- 095_bom_module_rbac.sql — RBAC catalog row + role grants for the new BOM module.
--
-- DEPLOY ORDER — THIS MIGRATION MUST RUN *BEFORE* THE CODE THAT DEPENDS ON IT
-- IS DEPLOYED. Both endpoints in app/modules/bom/router.py are gated on
-- require_permission("bom", action="view"), which resolves the single tuple
-- ('bom', NULL, NULL, 'view'). permission_service.check_permission walks
-- (module, sub, subsub, action) → (module, sub, NULL, action) →
-- (module, NULL, NULL, action); with no catalog row it finds nothing at ANY
-- level and denies. Ship the backend first and /api/v1/bom/* 403s for every
-- non-admin user until the next migration run.
--
-- IDEMPOTENT + NULL-SAFE — and the ON CONFLICT alone is NOT what makes it so.
-- auth_permission's UNIQUE (module, sub_module, sub_sub_module, action) uses
-- Postgres' default NULLS DISTINCT, so two NULLs never compare equal. The BOM
-- row has TWO NULLs (sub_module and sub_sub_module), which means a plain
-- `ON CONFLICT (…) DO NOTHING` would never suppress the re-insert, and
-- scripts/migrate.py re-executes every file on every deploy — one duplicate row
-- per deploy, surfacing as N identical checkboxes in the admin Roles &
-- Permissions tree (the same trap documented in 076_dedupe_permissions.sql,
-- which runs BEFORE this file and so cannot clean up after it). The
-- NOT EXISTS + IS NOT DISTINCT FROM guard below is what actually makes a re-run
-- a no-op; the ON CONFLICT is kept as a second line of defence for the day
-- somebody flips the constraint to NULLS NOT DISTINCT.
--
-- The role-grant statements need no such guard: auth_role_permission's PK is
-- (role_id, permission_id), neither of which is ever NULL, so ON CONFLICT DO
-- NOTHING is genuinely sufficient there.

-- ── 1. Catalog: the single tuple the BOM router gates on ────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, v.sub_module, v.sub_sub_module, v.action, v.description
  FROM (VALUES
      ('bom', NULL::text, NULL::text, 'view',
       'View the BOM module (rolled-up aggregate list + per-BOM detail)'),
      ('bom', NULL::text, NULL::text, 'create',
       'Create a BOM (header + lines + process route) via POST /api/v1/bom')
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

-- ── 2. Grants ─────────────────────────────────────────────────
-- ADMIN ONLY, for now, by request. Deliberately no viewer / planner /
-- production / npd_team grants: the catalog row above exists so the permission
-- is grantable the day that changes, but nobody holds it yet, so
-- /api/v1/bom/* answers only for admins (middleware short-circuits on
-- is_admin; every other role fails check_permission at all three fallback
-- levels).
--
-- The UI mirrors this in TWO places, and both are load-bearing:
--   * lib/modules.tsx MODULES "BOM" carries adminOnly: true
--   * planner has NO "bom" entry in ROLE_MODULE_SCOPE -- the scoped branch of
--     the /modules tile filter matches on that list alone and never consults
--     adminOnly, so a scope entry would re-expose the tile despite the flag.
-- To open the module up later: add the role grant here (or in a new file) AND
-- drop adminOnly, AND add the scope entry if the role is a scoped one.

-- admin: blanket parity with every other seed. Admins bypass the check in code
-- (middleware.py short-circuits on is_admin), but the row keeps the admin
-- Roles & Permissions tree honest.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'admin'
  AND p.module = 'bom'
ON CONFLICT DO NOTHING;
