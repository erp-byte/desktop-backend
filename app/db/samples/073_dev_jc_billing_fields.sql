-- 073_dev_jc_billing_fields.sql
-- Mirror the requisition billing checklist (returnable / non_returnable / paid + amount,
-- migration 072) onto npd_dev_job_cards as read-only INHERITED columns — copied from the
-- source requisition at create (like company_name / customer_name / dispatch fields in 064).
-- NULLABLE (no CHECK): a standalone/sourceless dev JC simply has no billing (NULL); the
-- source requisition remains the validated source of truth. Hand-apply via psql.
ALTER TABLE npd_dev_job_cards
    ADD COLUMN IF NOT EXISTS returnable     BOOLEAN,
    ADD COLUMN IF NOT EXISTS non_returnable BOOLEAN,
    ADD COLUMN IF NOT EXISTS paid           BOOLEAN,
    ADD COLUMN IF NOT EXISTS amount         NUMERIC(12,2);
