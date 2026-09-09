-- Stock Take module RBAC: a `stock_take` role, its permissions, and the grants.
--
-- Until now every /api/v1/stock-take/* endpoint was gated on get_current_user
-- alone — any authenticated user could read counted stock and post adjustments.
-- The module's own docstring said so, and leaned on the console tile being
-- admin-only. That is a UI convention, not an authorisation boundary: the tile
-- hides the link, it does not stop a request. This closes it properly, in the
-- database, where check_permission looks.
--
-- WHO GETS IN AFTERWARDS
--   * admins  — check_permission returns True for is_admin before it reads a
--               single row (permission_service.py:27), so they never needed a
--               grant. The explicit rows below exist anyway, matching what 095
--               did for `bom`, so the admin screen's permission matrix shows the
--               module rather than a blank where a grant should be.
--   * anyone holding the new `stock_take` role.
--   * nobody else.
--
-- THREE ACTIONS, NOT ONE. `view` covers the reads, `create` posting an
-- adjustment, `export` the two spreadsheet downloads. Splitting export off is
-- deliberate: an export is the one action that takes the whole stock position
-- out of the building, and it is useful to be able to grant looking without
-- granting that.
--
-- IDEMPOTENT VIA `NOT EXISTS`, NOT `ON CONFLICT`. The unique key is
-- (module, sub_module, sub_sub_module, action) and these rows carry NULL in the
-- two middle columns. Postgres treats NULLs as DISTINCT in a unique index by
-- default, so ON CONFLICT would happily insert a second identical row every run.

BEGIN;

-- ── The role ───────────────────────────────────────────────────────────
INSERT INTO auth_role (role_name, description, is_admin)
SELECT 'stock_take',
       'Stock take — physical count workspace: counted stock, adjustments, exports',
       FALSE
 WHERE NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'stock_take');

-- ── The permissions ────────────────────────────────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, NULL, NULL, v.action, v.description
  FROM (VALUES
        ('stock_take', 'view',
         'View the Stock Take module — counted stock, filter options, posting scope, per-article balances and the adjustment ledger'),
        ('stock_take', 'create',
         'Post a stock adjustment — an append-only ledger entry plus its write-back row in new_stock_entries'),
        ('stock_take', 'export',
         'Download the Stock Take spreadsheets — raw floor counts and the adjustment ledger')
       ) AS v(module, action, description)
 WHERE NOT EXISTS (
        SELECT 1 FROM auth_permission p
         WHERE p.module = v.module
           AND p.sub_module IS NULL
           AND p.sub_sub_module IS NULL
           AND p.action = v.action);

-- ── The grants: the new role, and every admin role ─────────────────────
-- No allowed_* arrays: scope stays a USER-level property (auth_user.allowed_
-- warehouses / allowed_floors), which is where the stock-take module already
-- reads it from. Setting role-level scope here would silently intersect with
-- the user's own and is not what anyone has been assigning.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r
  CROSS JOIN auth_permission p
 WHERE p.module = 'stock_take'
   AND p.sub_module IS NULL
   AND p.sub_sub_module IS NULL
   AND (r.role_name = 'stock_take' OR r.is_admin)
   AND NOT EXISTS (SELECT 1 FROM auth_role_permission x
                    WHERE x.role_id = r.role_id AND x.permission_id = p.permission_id);

COMMIT;
