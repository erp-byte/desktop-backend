-- 090_sku_lookup_permission.sql — let the NPD/sample roles search the SKU master.
--
-- WHY: GET /api/v1/so/sku-lookup is the ONLY data source behind the shared
-- ArticlePicker (modules/sample/_form.tsx) — both its Search typeahead and its
-- four-level Browse cascade — and that picker is what the NPD dev job-card trial
-- recipe (npd-development/_article-editor.tsx, job-cards/[id]), the NPD draft-BOM
-- section (sample/[id]/_npd-section.tsx) and the sample requisition form
-- (sample/new, sample/rm-issue-forms/new) use to add an article line.
--
-- It gated on the BARE `so` view permission, which only admin / so_creator /
-- viewer hold. npd_team carries sample-module grants exclusively (035 §3d, 075),
-- so every keystroke 403'd — and the picker renders a rejected lookup as an empty
-- list, so the operator saw "No matching articles." with no error. Same silent
-- dead end for business_head / sales / planner on the requisition form.
--
-- Granting bare `so:view` would have fixed it by handing the NPD team the whole
-- sales-order read surface (GET /so/{so_id}, the SO list, GST reconciliation).
-- Instead the lookup moves onto its own sub-module row, `so/sku_lookup/view`, and
-- the router gates on that. check_permission walks (so, sku_lookup, NULL, view)
-- -> (so, NULL, NULL, view), so the existing bare-`so:view` holders keep passing
-- through the fallback and this is a no-op for them — while the roles granted
-- below get the lookup and NOTHING else under `so`.
--
-- READ ONLY, and only the lookup: no create / edit / delete, no bare `so` row.
--
-- NOT granted here, deliberately: purchase_manager and store_head hit the same
-- 403 through the Material In walk-in intimation modal
-- (purchase/material-in/_WalkInIntimationModal.tsx), which is a different tile
-- and a separate call.
--
-- IDEMPOTENT + NULL-SAFE: auth_permission's UNIQUE (module, sub_module,
-- sub_sub_module, action) treats NULLs as DISTINCT, so ON CONFLICT DO NOTHING
-- would NOT stop a duplicate — sub_sub_module IS NULL on this row. The NOT EXISTS
-- guard is what makes a re-run a no-op (same trap documented in 084).

BEGIN;

-- ── 1. Catalog: the lookup's own permission tuple ───────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT 'so', 'sku_lookup', NULL, 'view',
       'Search the all_sku article master (shared ArticlePicker cascade)'
 WHERE NOT EXISTS (
     SELECT 1
       FROM auth_permission p
      WHERE p.module         = 'so'
        AND p.sub_module     = 'sku_lookup'
        AND p.sub_sub_module IS NULL
        AND p.action         = 'view'
 );

-- ── 2. Grants: the roles whose screens embed the ArticlePicker ──────────────
--   npd_team      — dev job-card trial recipe, NPD draft BOM, requisitions
--   business_head — sample requisition form (035 §3c)
--   sales         — sample requisition form (samples/081)
--   planner       — sample requisition form (035 §3g)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name IN ('npd_team', 'business_head', 'sales', 'planner')
   AND p.module         = 'so'
   AND p.sub_module     = 'sku_lookup'
   AND p.sub_sub_module IS NULL
   AND p.action         = 'view'
ON CONFLICT DO NOTHING;

-- admin bypasses check_permission entirely, but seed it for catalog parity with
-- the blanket grant in auth_schema.sql.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'admin'
   AND p.module         = 'so'
   AND p.sub_module     = 'sku_lookup'
   AND p.sub_sub_module IS NULL
   AND p.action         = 'view'
ON CONFLICT DO NOTHING;

COMMIT;
