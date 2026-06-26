-- 065_dispatch_roles.sql
-- FG Dispatch notification recipients. The dispatch To/CC lists target a few
-- roles that didn't exist yet — seed them idempotently so an admin can assign
-- users (until then they resolve to no email, and the send is a graceful no-op
-- for that role). 'stores' reuses the existing store_head role; business_head /
-- inventory_manager / production_manager already exist (auth_schema seed).
INSERT INTO auth_role (role_name, description, is_admin)
VALUES
    ('billing',           'Billing team — FG dispatch To recipient',       FALSE),
    ('candor_operations', 'Candor Operations — FG dispatch To recipient',  FALSE),
    ('operations_head',   'Operations Head — FG dispatch CC recipient',    FALSE)
ON CONFLICT (role_name) DO NOTHING;
