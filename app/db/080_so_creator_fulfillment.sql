-- 080_so_creator_fulfillment.sql
-- Fixes the two 403s a so_creator hits on the SO Creation page. 075 created the
-- role with `module = 'so'` grants only, but that page also calls two PRODUCTION
-- endpoints:
--   POST /production/fulfillment-v2/sync        -> production/fulfillment create
--        (the auto-sync fired after every Sales-Register upload + the Sync button)
--   POST /production/fulfillment-v2/by-so-lines -> production/fulfillment view
--        (per-line tick -> "Selected for Plan" panel; same gate as
--         GET /fulfillment-v2/{id}/detail, which the panel calls next)
-- view + create only — edit / delete / carryforward stay with planner + admin.
-- Idempotent. Safe to re-run.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'so_creator'
  AND p.module = 'production'
  AND p.sub_module = 'fulfillment'
  AND p.sub_sub_module IS NULL
  AND p.action IN ('view', 'create')
ON CONFLICT DO NOTHING;
