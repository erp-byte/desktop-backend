-- 081_sales_role.sql
-- New `sales` role — the requesting side of the NPD/sample flow. A sales user raises
-- NPD requisitions ON BEHALF OF a business head (picked from a dropdown on the form),
-- who still approves. So sales gets the SAME sample permissions as business_head MINUS
-- approve/convert: view + requisition create/edit (same grant shape as planner, 035 §3g).
-- Assign the role to users via the Admin screen. Applied out-of-band to the live DB
-- (like samples 068-080). Idempotent. Safe to re-run.
INSERT INTO auth_role (role_name, description, is_admin) VALUES
    ('sales', 'Sales — raises NPD/sample requisitions on behalf of a business head', FALSE)
ON CONFLICT (role_name) DO NOTHING;

INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
FROM auth_role r, auth_permission p
WHERE r.role_name = 'sales'
  AND p.module = 'sample'
  AND (p.action = 'view' OR (p.sub_module = 'requisition' AND p.action IN ('create', 'edit')))
ON CONFLICT DO NOTHING;
