-- 085_wa_po_intimation.sql
-- Inbound-WhatsApp state for the no-PO purchase intimation
-- (`purchase_without_po_intimation`, sent by purchase/services/po_intimation.py).
--
-- The template carries two quick replies — "PO Created & Uploaded" and "Don't Accept
-- the Material". A tap carries NO data of its own, only the button label plus
-- context.id = the wamid of the message it quotes, so the wamid has to be mapped back
-- to the walk-in arrival when it is sent. Mirrors wa_return_message (customer returns)
-- and wa_promote_message (NPD promote).
--
-- A tap also cannot carry the PO number the stores team needs, so "PO Created &
-- Uploaded" arms a pending capture keyed by the tapper's phone; their NEXT plain text
-- message is read as the PO number and forwarded to stores. Same shape as
-- wa_promote_pending (the promote-reject reason capture). Additive + idempotent.

CREATE TABLE IF NOT EXISTS wa_po_intimation_message (
    wamid          TEXT PRIMARY KEY,          -- Meta message id of the sent template
    transaction_no TEXT NOT NULL,             -- the WI-YYYYMMDDHHMMSS walk-in arrival
    vendor_name    TEXT,                      -- echoed to stores so they know the consignment
    invoice_no     TEXT,
    article_list   TEXT,
    wa_phone       TEXT,                      -- recipient (purchase), E.164 no '+'
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_po_intimation_message_txn
    ON wa_po_intimation_message(transaction_no);

CREATE TABLE IF NOT EXISTS wa_po_intimation_pending (
    wa_phone       TEXT PRIMARY KEY,          -- one pending capture per phone; re-prompt overwrites
    wamid          TEXT NOT NULL,             -- the intimation being answered
    transaction_no TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── The stores role ─────────────────────────────────────────────────────────
-- `store_head` is referenced by the frontend (ROLE_MODULE_SCOPE + the Purchase
-- landing gate) and by 075_rbac_notes_catalog.sql, but was never actually inserted
-- into auth_role — so "the stores role" had no rows to resolve. Create it and give it
-- Material In, matching what 075 already intends for it.
INSERT INTO auth_role (role_name, description, is_admin)
SELECT 'store_head', 'Stores', FALSE
 WHERE NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'store_head');

INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM auth_role r, auth_permission p
 WHERE r.role_name = 'store_head'
   AND p.module = 'purchase' AND p.sub_module = 'material_in'
ON CONFLICT DO NOTHING;
