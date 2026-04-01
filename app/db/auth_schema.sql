-- Auth & Access Control tables (v1)

-- =========================================================================
-- ROLES
-- =========================================================================

CREATE TABLE IF NOT EXISTS auth_role (
    role_id             SERIAL PRIMARY KEY,
    role_name           TEXT NOT NULL UNIQUE,
    description         TEXT,
    is_admin            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================================
-- USERS
-- =========================================================================

CREATE TABLE IF NOT EXISTS auth_user (
    user_id             SERIAL PRIMARY KEY,
    phone               TEXT NOT NULL UNIQUE,
    password_encrypted  TEXT NOT NULL,
    full_name           TEXT NOT NULL,
    email               TEXT,
    role_id             INT REFERENCES auth_role(role_id),
    entity              TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_auth_user_phone ON auth_user(phone);

-- =========================================================================
-- PERMISSIONS (hierarchical: module → sub_module → sub_sub_module × action)
-- =========================================================================

CREATE TABLE IF NOT EXISTS auth_permission (
    permission_id       SERIAL PRIMARY KEY,
    module              TEXT NOT NULL,
    sub_module          TEXT,
    sub_sub_module      TEXT,
    action              TEXT NOT NULL,
    description         TEXT,
    UNIQUE (module, sub_module, sub_sub_module, action)
);

-- =========================================================================
-- ROLE ↔ PERMISSION mapping (with scope restrictions)
-- =========================================================================

CREATE TABLE IF NOT EXISTS auth_role_permission (
    role_id             INT NOT NULL REFERENCES auth_role(role_id) ON DELETE CASCADE,
    permission_id       INT NOT NULL REFERENCES auth_permission(permission_id) ON DELETE CASCADE,
    allowed_entities    TEXT[],
    allowed_warehouses  TEXT[],
    allowed_floors      TEXT[],
    PRIMARY KEY (role_id, permission_id)
);

-- =========================================================================
-- SESSIONS (DB-managed, revocable)
-- =========================================================================

CREATE TABLE IF NOT EXISTS auth_session (
    session_id          SERIAL PRIMARY KEY,
    user_id             INT NOT NULL REFERENCES auth_user(user_id),
    token               TEXT NOT NULL UNIQUE,
    ip_address          TEXT,
    user_agent          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMPTZ NOT NULL,
    last_activity_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active           BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_auth_session_token ON auth_session(token);
CREATE INDEX IF NOT EXISTS idx_auth_session_user ON auth_session(user_id);

-- =========================================================================
-- SEED DATA: Default roles
-- =========================================================================

INSERT INTO auth_role (role_name, description, is_admin) VALUES
    ('admin',           'Full unrestricted access',                          TRUE),
    ('planner',         'Production planner — plans, fulfillment, MRP, AI',  FALSE),
    ('stores_manager',  'Stores manager — inventory, day-end, indents view', FALSE),
    ('team_leader',     'Team leader — job card execution',                  FALSE),
    ('qc_inspector',    'QC inspector — inspections, annexures, sign-offs',  FALSE),
    ('floor_manager',   'Floor manager — floor operations, discrepancy',     FALSE),
    ('purchase_manager','Purchase manager — indents, PO management',         FALSE),
    ('viewer',          'Read-only access to all modules',                   FALSE)
ON CONFLICT (role_name) DO NOTHING;

-- =========================================================================
-- SEED DATA: Default permissions
-- =========================================================================

-- Production module permissions
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES
    -- Fulfillment
    ('production', 'fulfillment', NULL,       'view',   'View fulfillment records'),
    ('production', 'fulfillment', NULL,       'create', 'Sync fulfillment'),
    ('production', 'fulfillment', NULL,       'edit',   'Revise fulfillment'),
    ('production', 'fulfillment', NULL,       'delete', 'Cancel fulfillment'),
    ('production', 'fulfillment', 'carryforward', 'create', 'Carry forward orders'),
    -- Plans
    ('production', 'plans',       NULL,       'view',   'View production plans'),
    ('production', 'plans',       NULL,       'create', 'Create/generate plans'),
    ('production', 'plans',       NULL,       'edit',   'Edit plan lines'),
    ('production', 'plans',       NULL,       'delete', 'Delete plan lines'),
    ('production', 'plans',       'approve',  'create', 'Approve plans'),
    ('production', 'plans',       'cancel',   'create', 'Cancel plans'),
    ('production', 'plans',       'revise',   'create', 'Revise plans (AI)'),
    -- MRP
    ('production', 'mrp',         NULL,       'view',   'Check material availability'),
    ('production', 'mrp',         NULL,       'create', 'Run MRP'),
    -- Indents
    ('production', 'indents',     NULL,       'view',   'View indents'),
    ('production', 'indents',     NULL,       'edit',   'Edit draft indents'),
    ('production', 'indents',     'send',     'create', 'Send indents'),
    ('production', 'indents',     'acknowledge', 'create', 'Acknowledge indents'),
    ('production', 'indents',     'link_po',  'create', 'Link indent to PO'),
    -- Alerts
    ('production', 'alerts',      NULL,       'view',   'View alerts'),
    ('production', 'alerts',      NULL,       'edit',   'Mark alerts read'),
    -- Orders
    ('production', 'orders',      NULL,       'view',   'View production orders'),
    ('production', 'orders',      NULL,       'create', 'Create production orders'),
    -- Job Cards
    ('production', 'job_cards',   NULL,       'view',   'View job cards'),
    ('production', 'job_cards',   'lifecycle','edit',   'Assign/start/complete job cards'),
    ('production', 'job_cards',   'output',   'create', 'Record output'),
    ('production', 'job_cards',   'annexures','create', 'Record annexure data'),
    ('production', 'job_cards',   'sign_offs','create', 'Sign off job cards'),
    ('production', 'job_cards',   'close',    'create', 'Close job cards'),
    ('production', 'job_cards',   'force_unlock','create','Force unlock job cards'),
    ('production', 'job_cards',   'generate', 'create', 'Generate job cards'),
    ('production', 'job_cards',   'receive_material','create','QR scan material receipt'),
    -- Inventory
    ('production', 'inventory',   NULL,       'view',   'View floor inventory'),
    ('production', 'inventory',   'move',     'create', 'Move material'),
    ('production', 'inventory',   'idle',     'create', 'Check idle materials'),
    -- Off-grade
    ('production', 'offgrade',    NULL,       'view',   'View off-grade inventory'),
    ('production', 'offgrade',    'rules',    'create', 'Manage off-grade rules'),
    -- Loss
    ('production', 'loss',        NULL,       'view',   'View loss analysis'),
    -- Day-end
    ('production', 'day_end',     NULL,       'view',   'View day-end summary'),
    ('production', 'day_end',     'dispatch', 'create', 'Submit dispatch'),
    ('production', 'day_end',     'scan',     'create', 'Submit balance scan'),
    ('production', 'day_end',     'reconcile','create', 'Reconcile balance scan'),
    ('production', 'day_end',     'missing',  'create', 'Check missing scans'),
    -- Discrepancy
    ('production', 'discrepancy', NULL,       'view',   'View discrepancies'),
    ('production', 'discrepancy', NULL,       'create', 'Report discrepancy'),
    ('production', 'discrepancy', 'resolve',  'create', 'Resolve discrepancy'),
    -- AI
    ('production', 'ai',          NULL,       'view',   'View AI recommendations'),
    ('production', 'ai',          NULL,       'edit',   'Submit AI feedback'),
    -- Yield
    ('production', 'yield',       NULL,       'view',   'View yield summary'),
    -- SO module
    ('so',         NULL,          NULL,       'view',   'View sales orders'),
    ('so',         NULL,          NULL,       'create', 'Upload/create sales orders'),
    ('so',         NULL,          NULL,       'edit',   'Update sales orders'),
    -- Purchase module
    ('purchase',   NULL,          NULL,       'view',   'View purchase orders'),
    ('purchase',   NULL,          NULL,       'create', 'Upload purchase orders'),
    ('purchase',   'receive',     NULL,       'create', 'Receive PO material'),
    ('purchase',   'boxes',       NULL,       'create', 'Add/update PO boxes'),
    -- Auth (admin only)
    ('auth',       'users',       NULL,       'view',   'View users'),
    ('auth',       'users',       NULL,       'create', 'Create users'),
    ('auth',       'users',       NULL,       'edit',   'Edit users'),
    ('auth',       'users',       NULL,       'delete', 'Deactivate users'),
    ('auth',       'roles',       NULL,       'view',   'View roles'),
    ('auth',       'roles',       NULL,       'create', 'Create roles'),
    ('auth',       'roles',       NULL,       'edit',   'Edit role permissions')
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

-- =========================================================================
-- SEED DATA: Assign all permissions to admin role
-- =========================================================================

INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'admin'
ON CONFLICT DO NOTHING;

-- Viewer role: all view permissions
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'viewer' AND p.action = 'view'
ON CONFLICT DO NOTHING;

-- Planner role
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'planner'
  AND (
    p.module = 'production' AND p.sub_module IN ('plans', 'fulfillment', 'mrp', 'indents', 'ai', 'alerts', 'orders', 'loss', 'yield')
    OR (p.module = 'production' AND p.action = 'view')
  )
ON CONFLICT DO NOTHING;

-- Team leader role
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'team_leader'
  AND p.module = 'production'
  AND (
    (p.sub_module = 'job_cards')
    OR (p.action = 'view')
  )
ON CONFLICT DO NOTHING;

-- Stores manager role
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'stores_manager'
  AND p.module = 'production'
  AND (
    p.sub_module IN ('inventory', 'day_end', 'offgrade')
    OR p.action = 'view'
  )
ON CONFLICT DO NOTHING;

-- QC inspector role
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'qc_inspector'
  AND p.module = 'production'
  AND (
    (p.sub_module = 'job_cards' AND p.sub_sub_module IN ('annexures', 'sign_offs'))
    OR p.action = 'view'
  )
ON CONFLICT DO NOTHING;

-- Floor manager role
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'floor_manager'
  AND p.module = 'production'
  AND (
    p.sub_module IN ('inventory', 'day_end', 'discrepancy')
    OR (p.sub_module = 'job_cards' AND p.action = 'view')
    OR p.action = 'view'
  )
ON CONFLICT DO NOTHING;

-- Purchase manager role
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'purchase_manager'
  AND (
    p.module = 'purchase'
    OR (p.module = 'production' AND p.sub_module IN ('indents', 'alerts') AND p.action IN ('view', 'create'))
    OR (p.module = 'production' AND p.action = 'view')
  )
ON CONFLICT DO NOTHING;
