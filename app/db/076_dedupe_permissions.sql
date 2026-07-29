-- 076_dedupe_permissions.sql
-- Collapse duplicate auth_permission rows (and their duplicate role grants).
--
-- Root cause: auth_permission's UNIQUE (module, sub_module, sub_sub_module,
-- action) uses Postgres' default NULLS DISTINCT, so two NULLs never compare
-- equal. That means ON CONFLICT DO NOTHING does NOT suppress a re-insert of any
-- permission whose sub_module or sub_sub_module is NULL (e.g.
-- production/job_cards/NULL/view, production/loss/NULL/view,
-- production/mrp/NULL/create). scripts/migrate.py re-runs every seed file on
-- each deploy, so those NULL-column rows pile up one copy per deploy, surfacing
-- as N identical checkboxes per action in the admin Roles and Permissions tree.
--
-- This migration is registered LAST and is idempotent: keep the lowest
-- permission_id per (module, sub_module, sub_sub_module, action), repoint every
-- role grant onto that canonical id, delete the duplicate rows. Whatever the
-- earlier files re-inserted this run is cleaned up before the run ends, and
-- re-running on an already-clean table is a no-op.
--
-- Verify after running (expect zero rows):
--   SELECT module, sub_module, sub_sub_module, action, COUNT(*)
--   FROM auth_permission GROUP BY 1,2,3,4 HAVING COUNT(*) > 1

-- 1. Per (role, tuple) keep only the grant with the lowest permission_id and
--    drop the rest, so repointing in step 2 cannot collide on the
--    (role_id, permission_id) primary key.
WITH ranked AS (
    SELECT rp.role_id, rp.permission_id,
           row_number() OVER (
               PARTITION BY rp.role_id, p.module, p.sub_module, p.sub_sub_module, p.action
               ORDER BY rp.permission_id
           ) AS rn
    FROM auth_role_permission rp
    JOIN auth_permission p ON p.permission_id = rp.permission_id
)
DELETE FROM auth_role_permission rp
USING ranked
WHERE rp.role_id = ranked.role_id
  AND rp.permission_id = ranked.permission_id
  AND ranked.rn > 1;

-- 2. Repoint each surviving grant onto the canonical (lowest-id) permission for
--    its tuple. Window PARTITION treats NULLs as equal, so the NULL-column
--    duplicates group correctly (unlike the unique index).
WITH canon AS (
    SELECT permission_id,
           MIN(permission_id) OVER (
               PARTITION BY module, sub_module, sub_sub_module, action
           ) AS keep_id
    FROM auth_permission
)
UPDATE auth_role_permission rp
SET permission_id = canon.keep_id
FROM canon
WHERE rp.permission_id = canon.permission_id
  AND canon.permission_id <> canon.keep_id;

-- 3. Delete the duplicate permission rows (the canonical id per tuple survives).
WITH canon AS (
    SELECT permission_id,
           MIN(permission_id) OVER (
               PARTITION BY module, sub_module, sub_sub_module, action
           ) AS keep_id
    FROM auth_permission
)
DELETE FROM auth_permission p
USING canon
WHERE p.permission_id = canon.permission_id
  AND canon.permission_id <> canon.keep_id;
