-- =========================================================================
-- 005_po_rebuild.sql
-- Adds the schema needed for the documented /api/v1/po endpoints:
--   • soft-delete columns on po_header (deleted_at/by/reason) — keeps the
--     existing `is_deleted` boolean as a denormalised flag for backward compat
--   • po_event_log table — append-only audit for create/update/delete
--   • permission seeds: purchase.po.{create,edit,read,delete}
--   • STORE_HEAD role (required by the soft-delete gate when po_box>0)
--
-- Idempotent. Safe to re-run. Requires PostgreSQL ≥ 11.
--
-- Cost notes:
--   • ADD COLUMN IF NOT EXISTS … (no NOT NULL, no DEFAULT) is instant on PG 11+.
--   • The is_deleted ↔ deleted_at backfill UPDATE scans po_header once.
--     Cheap unless the table grows past ~100k rows.
-- =========================================================================

-- ── po_header soft-delete columns ────────────────────────────────────────

ALTER TABLE po_header
    ADD COLUMN IF NOT EXISTS deleted_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by     TEXT,
    ADD COLUMN IF NOT EXISTS delete_reason  TEXT;

-- Backfill so existing is_deleted=true rows have a deleted_at timestamp the
-- queries can filter on. Use created_at as a placeholder when the original
-- delete time is unknown.
UPDATE po_header
   SET deleted_at = COALESCE(deleted_at, created_at, NOW())
 WHERE COALESCE(is_deleted, FALSE) = TRUE
   AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_po_header_deleted_at  ON po_header(deleted_at);

-- HI-01: partial UNIQUE on (entity, po_number) for live rows.
-- A plain index lets two concurrent commit batches both pass the
-- "no existing row" check and INSERT, producing duplicate headers that
-- break update-by-transaction_no. The partial predicate scopes the
-- constraint to *live* rows so soft-deleted POs can be re-created.
-- WARNING: if any live duplicates exist today this CREATE will fail —
-- run `SELECT entity, po_number, COUNT(*) FROM po_header WHERE deleted_at IS NULL AND po_number IS NOT NULL GROUP BY 1,2 HAVING COUNT(*) > 1;` first.
CREATE UNIQUE INDEX IF NOT EXISTS uq_po_header_live_entity_pono
    ON po_header(entity, po_number)
 WHERE deleted_at IS NULL AND po_number IS NOT NULL;

-- ── po_event_log: append-only audit ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS po_event_log (
    event_id        BIGSERIAL    PRIMARY KEY,
    transaction_no  TEXT         NOT NULL,
    entity          TEXT         NOT NULL,
    event_type      TEXT         NOT NULL,    -- created | updated | soft_deleted | restored
    actor_user_id   TEXT,                     -- nullable for system events
    actor_role      TEXT,
    reason          TEXT,
    payload         JSONB,                    -- before/after diffs etc
    occurred_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_po_event_log_txn        ON po_event_log(transaction_no, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_po_event_log_entity     ON po_event_log(entity, occurred_at DESC);

-- ── Permission seeds for the new endpoints ───────────────────────────────
--
-- The pre-existing rows under (purchase, NULL, NULL, *) stay in place — the
-- legacy /api/v1/purchase/* router still uses them. The new contract uses
-- (purchase, po, NULL, *) so the two paths are independently revocable.

INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES
    ('purchase', 'po', NULL, 'read',   'View purchase orders (list/detail/lines)'),
    ('purchase', 'po', NULL, 'create', 'Preview/parse PO uploads'),
    ('purchase', 'po', NULL, 'edit',   'Commit (create/update/upsert) POs'),
    ('purchase', 'po', NULL, 'delete', 'Soft-delete POs')
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

-- ── STORE_HEAD role ──────────────────────────────────────────────────────
--
-- Soft-delete on a PO with weighed boxes (po_box > 0) requires this role
-- (or ADMIN). Spec: 403 StoreHeadApprovalRequired.

INSERT INTO auth_role (role_name, description, is_admin) VALUES
    ('store_head', 'Stores head — approves deletes of POs with weighed goods', FALSE)
ON CONFLICT (role_name) DO NOTHING;

-- Grant store_head all four purchase.po.* permissions
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'purchase' AND p.sub_module = 'po'
ON CONFLICT DO NOTHING;

-- Grant admin all four purchase.po.* permissions (admin should already get
-- everything via the catch-all seed in auth_schema.sql, but be explicit)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'admin'
   AND p.module = 'purchase' AND p.sub_module = 'po'
ON CONFLICT DO NOTHING;

-- Grant purchase_manager read/create/edit (no delete by default)
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'purchase_manager'
   AND p.module = 'purchase' AND p.sub_module = 'po'
   AND p.action IN ('read', 'create', 'edit')
ON CONFLICT DO NOTHING;

-- Grant viewer read-only
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'viewer'
   AND p.module = 'purchase' AND p.sub_module = 'po'
   AND p.action = 'read'
ON CONFLICT DO NOTHING;
