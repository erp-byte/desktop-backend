-- ===========================================================================
-- 136_debtors_collection_role.sql
-- The Debtors Collection role, its permissions, and the grants.
--
-- Debtors Collection (the DPD view, AI-agent calls to customers, party edits,
-- the Excel export and the Tally upload) has been admin-only: the web app's
-- Route Handlers under /api/collections asked /api/v1/auth/me whether the
-- caller was an admin and refused everyone else. This gives the module a role
-- of its own, so collections staff can work it without being made admins.
--
-- WHERE THESE ARE CHECKED. Not in this backend: the module's data lives in the
-- separate collections service, which takes one shared key and cannot verify
-- an ERP login. The web app's Route Handlers (src/lib/server/collections.ts)
-- read the caller's permissions from /me and check the one each request needs
-- before they add that key. /me already returns every permission a user's
-- roles grant, so no backend code changes with this file.
--
-- SIX ACTIONS, ALL MODULE-LEVEL (sub_module and sub_sub_module NULL):
--   view    the parties, the DPD report, call summaries and conversations,
--           and a placed call's progress
--   call    ring a party with the AI collections agent
--   edit    change a party's row, contact number or remarks (adding a call
--           summary to the remarks is an edit)
--   export  download a tab's parties to Excel
--   upload  replace a tab's parties from a Tally Group Outstandings export
--   delete  delete a call summary
--
-- WHO GETS WHAT
--   * debtors_collection - view, call, edit, export: the day-to-day work.
--   * admins - all six. They pass on their flag anyway (the web app checks it
--     first, like check_permission does); the rows exist so the admin screen's
--     permission matrix shows the module, as 102 did for stock_take.
--   upload and delete stay with admins on purpose: an upload replaces the
--   whole tab, and a deleted summary is gone. Either can be granted to the
--   role later on the admin screen.
--
-- IDEMPOTENT VIA `NOT EXISTS`, NOT `ON CONFLICT`. The unique key is
-- (module, sub_module, sub_sub_module, action) and these rows carry NULL in the
-- two middle columns. Postgres treats NULLs as DISTINCT in a unique index by
-- default, so ON CONFLICT would insert a second identical row every run.
-- ===========================================================================

BEGIN;

-- -- The role ---------------------------------------------------------------
INSERT INTO auth_role (role_name, description, is_admin)
SELECT 'debtors_collection',
       'Debtors Collection - DPD view, AI-agent calls, party edits, Excel export',
       FALSE
 WHERE NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'debtors_collection');

-- -- The permissions --------------------------------------------------------
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, NULL, NULL, v.action, v.description
  FROM (VALUES
        ('debtors_collection', 'view',
         'View Debtors Collection - parties, DPD ageing, call summaries and conversations'),
        ('debtors_collection', 'call',
         'Ring a party with the AI collections agent'),
        ('debtors_collection', 'edit',
         'Change a party''s row, contact number or remarks'),
        ('debtors_collection', 'export',
         'Download a Debtors Collection tab to Excel'),
        ('debtors_collection', 'upload',
         'Replace a Debtors Collection tab from a Tally Group Outstandings export'),
        ('debtors_collection', 'delete',
         'Delete a call summary')
       ) AS v(module, action, description)
 WHERE NOT EXISTS (
        SELECT 1 FROM auth_permission p
         WHERE p.module = v.module
           AND p.sub_module IS NULL
           AND p.sub_sub_module IS NULL
           AND p.action = v.action);

-- -- The grants: the role gets the day-to-day four ---------------------------
-- No allowed_* arrays: the collections service has no warehouse or floor to
-- scope by.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r
  CROSS JOIN auth_permission p
 WHERE r.role_name = 'debtors_collection'
   AND p.module = 'debtors_collection'
   AND p.sub_module IS NULL
   AND p.sub_sub_module IS NULL
   AND p.action IN ('view', 'call', 'edit', 'export')
   AND NOT EXISTS (SELECT 1 FROM auth_role_permission x
                    WHERE x.role_id = r.role_id AND x.permission_id = p.permission_id);

-- -- ...and every admin role all six -----------------------------------------
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r
  CROSS JOIN auth_permission p
 WHERE r.is_admin
   AND p.module = 'debtors_collection'
   AND p.sub_module IS NULL
   AND p.sub_sub_module IS NULL
   AND NOT EXISTS (SELECT 1 FROM auth_role_permission x
                    WHERE x.role_id = r.role_id AND x.permission_id = p.permission_id);

COMMIT;
