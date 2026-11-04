-- ===========================================================================
-- 128_po_box_request_key.sql
-- Material In: apply each box save once.
--
-- The inward page gives every new box section, and every batch of boxes added
-- to an existing section, a random key, and sends it with
-- POST /api/v1/purchase/{txn}/boxes. The server records the key in the same
-- transaction as the boxes, and skips a payload whose key is already recorded.
-- So a retry after the reply was lost (the boxes were saved, but the browser
-- never heard back) doesn't add the boxes a second time.
--
-- The backend looks for this table and skips the check while it is missing, so
-- this can run before or after the backend deploys. Idempotent; one transaction.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS po_box_request_key (
    transaction_no  TEXT        NOT NULL,
    client_key      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (transaction_no, client_key)
);

COMMIT;
