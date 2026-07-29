-- 075_rbac_notes_catalog.sql
-- RBAC catalog + role grants for the "Roles & Permission" spec (Purchase /
-- Production / NPD). The middleware (auth/middleware.py::require_permission,
-- permission_service.check_permission) is UNCHANGED -- this only adds the
-- 4-tuple permission rows the routers now gate on, plus the role grants.
--
-- Idempotent: ON CONFLICT DO NOTHING on both the catalog UNIQUE
-- (module, sub_module, sub_sub_module, action) and the role_permission PK.
-- Runs AFTER auth_schema.sql, so every grant below is re-stated here (the
-- auth_schema admin/viewer/etc. blanket grants ran before these rows existed).

-- =========================================================================
-- CATALOG -- new permission rows (existing rows are noted, not re-inserted)
-- =========================================================================

INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES
    -- -- PURCHASE -------------------------------------------------------
    -- PO upload  -> purchase.po.{read,create,edit,delete}  (already seeded)
    -- Material In  [POInt | view | QCInt | create | update]
    ('purchase', 'material_in', NULL,     'view',   'View stores receiving / material-in'),
    ('purchase', 'material_in', NULL,     'create', 'Add stores boxes/sections on receipt'),
    ('purchase', 'material_in', NULL,     'edit',   'Stores receive header / lines / boxes'),
    ('purchase', 'material_in', 'point',  'create', 'Send PO-based arrival intimation (POInt)'),
    ('purchase', 'material_in', 'qcint',  'create', 'Send QC intimation (QCInt)'),
    -- Vendor Mgmt  -> vendor.{master,banking,document,contract}.*  (already seeded)

    -- -- PRODUCTION -----------------------------------------------------
    -- SO Creation  -> so.{view,create,edit} already seeded, add delete
    ('so', NULL, NULL, 'delete', 'Delete / cancel a sales order'),
    -- Planning  -> production.plans.{view,create,edit,delete,approve,cancel,revise}
    --            already seeded, add dispatch
    ('production', 'plans', 'dispatch', 'create', 'Dispatch FG against a plan line'),
    -- Force-reassign a batch to another SO (replaces a broken hand-rolled auth check)
    ('production', 'inventory', 'force_reassign', 'create', 'Force-reassign a batch to another SO'),
    -- Job (batch-wise). job_cards.{view,output,sign_offs,force_unlock,generate,
    --   receive_material} already seeded, add the full-granularity areas:
    ('production', 'job_cards', 'stage_chain',  'view',     'View the job-card stage chain'),
    ('production', 'job_cards', 'material_scan','scan',     'Scan RM / SFG boxes to a job card'),
    ('production', 'job_cards', 'overview',     'assign',   'Assign team to a job card'),
    ('production', 'job_cards', 'overview',     'start',    'Start a job card / batch'),
    ('production', 'job_cards', 'overview',     'complete', 'Complete a job card / stage'),
    ('production', 'job_cards', 'overview',     'close',    'Close a job card'),
    ('production', 'job_cards', 'accounting',   'view',     'View job-card accounting'),
    ('production', 'job_cards', 'accounting',   'create',   'Create job-card accounting entries'),
    ('production', 'job_cards', 'accounting',   'edit',     'Edit consumption / byproducts'),
    ('production', 'job_cards', 'accounting',   'close',    'Finalise job-card accounting'),
    ('production', 'job_cards', 'quality',      'view',     'View QC annexures'),
    ('production', 'job_cards', 'quality',      'create',   'Add QC annexure readings'),
    ('production', 'job_cards', 'quality',      'edit',     'Edit / delete QC annexure readings'),
    ('production', 'job_cards', 'remark',       'view',     'View remarks'),
    ('production', 'job_cards', 'remark',       'create',   'Add remarks'),
    ('production', 'job_cards', 'remark',       'edit',     'Edit / delete remarks'),
    ('production', 'job_cards', 'box_printing', 'view',     'View / download box & carton labels'),
    ('production', 'job_cards', 'box_printing', 'create',   'Create WIP / FG boxes & print labels'),

    -- -- NPD (sample) ---------------------------------------------------
    -- Sample requisition  -> sample.requisition.{create,edit} already seeded, add delete
    ('sample', 'requisition', NULL, 'delete', 'Cancel / delete a sample requisition'),
    -- Development JC  -> sample.npd.create already seeded, add edit
    ('sample', 'npd', NULL, 'edit',   'Edit a development job card'),
    -- Browse JC delete -> sample.npd.delete
    ('sample', 'npd', NULL, 'delete', 'Cancel / delete a development job card'),

    -- -- PRODUCTION (router RBAC hardening) --------------------------------
    -- Inventory writes at the sub_module root: internal-issue + legacy-import
    -- (create) and batch state-changes flag/block/reject/resolve/return/approve
    -- (edit). inventory.view / move / idle are already seeded in auth_schema.
    ('production', 'inventory', NULL, 'create', 'Create internal issue / legacy inventory import'),
    ('production', 'inventory', NULL, 'edit',   'Batch state-change flag/block/reject/resolve/return/approve-issue'),
    -- Store allocation controller (shop-floor material allocation)
    ('production', 'store', NULL, 'view',   'View store pending allocations / dashboard'),
    ('production', 'store', NULL, 'create', 'Store suggest-alternative allocation'),
    ('production', 'store', NULL, 'edit',   'Store decide / verify-floor-stock'),
    -- Lot picker / issuance
    ('production', 'lots', NULL, 'view',   'View lots / other-warehouse lots'),
    ('production', 'lots', NULL, 'create', 'Force-assign / FIFO-skip a lot'),
    -- Production indents (FG/SFG)
    ('production', 'production_indents', NULL,      'view',   'View production indents'),
    ('production', 'production_indents', NULL,      'create', 'Create / submit / create-internal-order for production indents'),
    ('production', 'production_indents', 'approve', 'create', 'Approve / return / cancel a production indent'),
    -- RTV disposition
    ('production', 'rtv', NULL, 'view',   'View RTV dispositions'),
    ('production', 'rtv', NULL, 'create', 'Assign RTV disposition / approve discard')
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

-- =========================================================================
-- ROLES -- create so_creator (frontend already scopes it, make it real in DB)
-- =========================================================================

INSERT INTO auth_role (role_name, description) VALUES
    ('so_creator', 'SO Creation -- create / view / update / delete sales orders'),
    ('store_head', 'Stores / Material-In clerk -- receive material, manage boxes')
ON CONFLICT (role_name) DO NOTHING;

-- =========================================================================
-- GRANTS  (set-based, ON CONFLICT DO NOTHING)
-- =========================================================================

-- admin: blanket over EVERY permission (re-run so it picks up the new rows,
-- admin also bypasses the check in code).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'admin'
ON CONFLICT DO NOTHING;

-- viewer: every action='view' (re-run picks up the new *.view rows).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'viewer' AND p.action = 'view'
ON CONFLICT DO NOTHING;

-- so_creator: full SO lifecycle.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'so_creator' AND p.module = 'so'
ON CONFLICT DO NOTHING;

-- purchase_manager + store_head: Material In (stores receiving + intimations).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name IN ('purchase_manager', 'store_head')
  AND p.module = 'purchase' AND p.sub_module = 'material_in'
ON CONFLICT DO NOTHING;

-- planner: full Planning + generate/view/stage-chain on Job (no shop-floor edit).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'planner'
  AND (
    (p.module = 'production' AND p.sub_module = 'plans')
    OR (p.module = 'production' AND p.sub_module = 'job_cards'
        AND (p.sub_sub_module IN ('generate', 'stage_chain') OR p.action = 'view'))
  )
ON CONFLICT DO NOTHING;

-- floor_manager: the full batch-wise shop-floor execution surface.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'floor_manager'
  AND p.module = 'production' AND p.sub_module = 'job_cards'
  AND (
    p.action = 'view'
    OR p.sub_sub_module IN ('stage_chain', 'material_scan', 'overview', 'accounting',
                            'quality', 'remark', 'box_printing', 'sign_offs',
                            'receive_material', 'output', 'force_unlock')
  )
ON CONFLICT DO NOTHING;

-- team_leader: every job_cards action (existing intent, re-run for the new rows).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'team_leader'
  AND p.module = 'production' AND p.sub_module = 'job_cards'
ON CONFLICT DO NOTHING;

-- qc_inspector: the Quality annexure surface. (QCInt / Send-QC-intimation is a
-- stores-side trigger owned by purchase_manager/store_head, not QC, so it is
-- intentionally NOT granted here.)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'qc_inspector'
  AND p.module = 'production' AND p.sub_module = 'job_cards' AND p.sub_sub_module = 'quality'
ON CONFLICT DO NOTHING;

-- inventory_manager: force-reassign a batch (was gated by a broken inline check).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'inventory_manager'
  AND p.module = 'production' AND p.sub_module = 'inventory' AND p.sub_sub_module = 'force_reassign'
ON CONFLICT DO NOTHING;

-- npd_team: full sample/NPD module (re-run for the new edit/delete rows).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'npd_team' AND p.module = 'sample'
ON CONFLICT DO NOTHING;

-- inventory_manager: full inventory + store + lots + rtv + offgrade + day-end
-- surface (floor-inventory is the inventory sub_module). The force_reassign
-- grant above is a subset of this superset, which re-runs idempotently.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'inventory_manager'
  AND p.module = 'production'
  AND p.sub_module IN ('inventory', 'store', 'lots', 'rtv', 'offgrade', 'day_end')
ON CONFLICT DO NOTHING;

-- planner: demand + material planning surface (fulfillment / mrp / indents /
-- alerts / orders / production_indents). Plans + job-card generate are granted
-- in the planner block above.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'planner'
  AND p.module = 'production'
  AND p.sub_module IN ('fulfillment', 'mrp', 'indents', 'alerts', 'orders', 'production_indents')
ON CONFLICT DO NOTHING;

-- floor_manager: shop-floor allocation + day-end, view + create only.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'floor_manager'
  AND p.module = 'production'
  AND p.sub_module IN ('store', 'day_end')
  AND p.action IN ('view', 'create')
ON CONFLICT DO NOTHING;
