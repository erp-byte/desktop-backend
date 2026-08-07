-- 087_store_head_po_read.sql — let stores actually open Material In.
--
-- Material In is po_header-based: the list is GET /api/v1/po and the inward screen
-- pulls GET /api/v1/po/{txn}, /lines, /receipt-summary and GET /api/v1/purchase/{txn}.
-- Every one of those gates on purchase.po.READ, not purchase.material_in.*, so
-- store_head (granted only material_in.* by 085) got HTTP 403 on all three dashboard
-- sections — "Failed to load POs (HTTP 403)".
--
-- READ ONLY, deliberately: PO Upload gates on purchase.po.create (preview/upload),
-- .edit (commit) and .delete, none of which are granted here — so stores can read the
-- purchase orders it receives against but cannot upload, commit or delete one. The
-- frontend additionally hides the PO Upload + Vendor Management sub-tiles from
-- store_head and bounces their routes (ROLE_MODULE_SCOPE["store_head"] =
-- ["purchase/material-in"]); this migration is the server half of the same rule.
--
-- Idempotent: auth_role_permission's PK is (role_id, permission_id) with no NULLs,
-- so ON CONFLICT DO NOTHING is sufficient here (unlike the auth_permission catalog
-- inserts in 084, where NULL sub_sub_module defeats the UNIQUE constraint).

INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'purchase'
   AND p.sub_module = 'po'
   AND p.action = 'read'
ON CONFLICT DO NOTHING;
