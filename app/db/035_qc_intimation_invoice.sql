-- =========================================================================
-- 035_qc_intimation_invoice.sql
--
-- Adds invoice_no column to qc_intimation so the Material In "Send
-- intimation" call can persist the invoice reference alongside each
-- arrival event row.
--
-- Fully idempotent — safe to re-run on any DB state. PostgreSQL >= 13.
-- =========================================================================

ALTER TABLE qc_intimation ADD COLUMN IF NOT EXISTS invoice_no TEXT;
