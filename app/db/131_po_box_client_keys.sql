-- ===========================================================================
-- 131_po_box_client_keys.sql
-- Browser keys on po_section and po_box, so Material In can save one box at a
-- time (print / Update on a single box) without a retry adding it twice.
--
-- The browser makes one key per new section and one per box. POST
-- /purchase/{txn}/box and POST /purchase/{txn}/boxes look a key up before
-- creating: a section key already saved is appended to, a box key already
-- saved is that box. The backend skips the lookups while these columns are
-- missing, so this may run before or after the deploy. Idempotent; nullable,
-- existing rows are untouched.
-- ===========================================================================

BEGIN;

ALTER TABLE po_section ADD COLUMN IF NOT EXISTS client_key TEXT;
ALTER TABLE po_box     ADD COLUMN IF NOT EXISTS client_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_po_section_client_key
    ON po_section (transaction_no, client_key) WHERE client_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_po_box_client_key
    ON po_box (transaction_no, client_key) WHERE client_key IS NOT NULL;

COMMIT;
