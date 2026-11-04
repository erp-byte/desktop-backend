-- ===========================================================================
-- 130_po_header_consignee_address.sql
-- Consignee (ship-to) address per PO, read from the "Consignee Address"
-- column of the PO Book upload, beside Particulars.
--
-- Apply BEFORE the backend that writes it deploys: the PO commit inserts this
-- column, so without it every PO in an upload fails. Idempotent; nullable, so
-- existing POs are untouched.
-- ===========================================================================

BEGIN;

ALTER TABLE po_header ADD COLUMN IF NOT EXISTS consignee_address TEXT;

COMMIT;
