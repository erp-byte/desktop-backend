-- =========================================================================
-- Migration 035: Sample Issuing module — auth roles & permissions
--
-- Per the approved checklist ("Option B"), add the two roles the Sample
-- Issuing module needs that did NOT already exist:
--   • business_head — approves sample requisitions + conversions, signs gate pass
--   • npd_team      — authors / edits NPD draft BOMs, proposes promotion
--
-- NOTE: inventory_manager already exists (migration 028 renamed
--       stores_manager -> inventory_manager) and is reused as-is for the
--       INV_MGR_* approval stages. floor_manager is reused for PRODUCTION_ACK.
--
-- Also seeds the `sample` permission catalog and grants it to the relevant
-- roles. admin keeps full access via its blanket grant; viewer gets read.
--
-- The actual stage->role routing is data-driven via sample_approval_role_map
-- (migration 037); these grants are the coarse module-level gate the routers
-- check (mirrors how the production module gates).
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- 1. New roles ------------------------------------------------------------
INSERT INTO auth_role (role_name, description, is_admin) VALUES
    ('business_head', 'Business head — approves sample requisitions & conversions, signs gate passes', FALSE),
    ('npd_team',      'NPD team — authors NPD draft BOMs, proposes promotion to live BOM',             FALSE)
ON CONFLICT (role_name) DO NOTHING;

-- 2. Sample permission catalog -------------------------------------------
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES
    -- view stays module-level (all sample data is broadly viewable).
    ('sample', NULL,             NULL,      'view',   'View sample requisitions & gate passes'),
    -- Requestor create/edit live under a sub_module so they do NOT occupy the
    -- bare (sample, NULL, create/edit) slot. check_permission falls back
    -- sub->NULL, so a bare module-level create grant would otherwise satisfy
    -- EVERY stage endpoint (approve / gate_pass / convert ...). Keeping these
    -- under 'requisition' preserves separation of duties.
    ('sample', 'requisition',    NULL,      'create', 'Create sample requisitions'),
    ('sample', 'requisition',    NULL,      'edit',   'Edit / resubmit draft sample requisitions'),
    ('sample', 'approve',        NULL,      'create', 'Business-head approval / rejection (BH_APPROVAL)'),
    ('sample', 'production_ack', NULL,      'create', 'Production acknowledgement (PRODUCTION_ACK)'),
    ('sample', 'inv_signoff',    NULL,      'create', 'Inventory-manager verification & sign-off (INV_MGR_*)'),
    ('sample', 'npd',            NULL,      'create', 'Create / edit NPD draft BOMs'),
    ('sample', 'npd',            'promote', 'create', 'Promote NPD draft BOM to a live BOM'),
    ('sample', 'gate_pass',      NULL,      'create', 'Issue / print sample gate pass'),
    ('sample', 'convert',        NULL,      'create', 'Convert internal sample to external gate pass')
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

-- 3. Grants ---------------------------------------------------------------
-- 3a. admin already gets every permission via the blanket grant in
--     auth_schema.sql; re-run it so the new `sample` rows are covered too.
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'admin' AND p.module = 'sample'
ON CONFLICT DO NOTHING;

-- 3b. viewer — read only
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'viewer' AND p.module = 'sample' AND p.action = 'view'
ON CONFLICT DO NOTHING;

-- 3c. business_head — view + raise/edit requisitions + BH approval + conversion approval
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'business_head'
  AND p.module = 'sample'
  AND (
        p.action = 'view'
     OR p.sub_module IN ('approve', 'convert')
     OR (p.sub_module = 'requisition' AND p.action IN ('create', 'edit'))
  )
ON CONFLICT DO NOTHING;

-- 3d. npd_team — view + raise/edit requisitions + NPD draft BOM authoring & promotion
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'npd_team'
  AND p.module = 'sample'
  AND (
        p.action = 'view'
     OR p.sub_module = 'npd'
     OR (p.sub_module = 'requisition' AND p.action IN ('create', 'edit'))
  )
ON CONFLICT DO NOTHING;

-- 3e. inventory_manager — view + INV verification/sign-off + gate-pass issuance
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'inventory_manager'
  AND p.module = 'sample'
  AND (
        p.action = 'view'
     OR p.sub_module IN ('inv_signoff', 'gate_pass')
  )
ON CONFLICT DO NOTHING;

-- 3f. floor_manager — view + production acknowledgement (FG / NPD sample jobcards)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'floor_manager'
  AND p.module = 'sample'
  AND (
        p.action = 'view'
     OR p.sub_module = 'production_ack'
  )
ON CONFLICT DO NOTHING;

-- 3g. planner — view + raise/edit requisitions (Basis RM / FG requestor)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'planner'
  AND p.module = 'sample'
  AND (p.action = 'view' OR (p.sub_module = 'requisition' AND p.action IN ('create', 'edit')))
ON CONFLICT DO NOTHING;

COMMIT;
