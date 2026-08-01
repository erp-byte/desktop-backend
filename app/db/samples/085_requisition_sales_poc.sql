-- 085_requisition_sales_poc.sql
-- Requisition requestor / sales-POC split:
--   • requestor_user_id     = the SELECTED business head the request is raised FOR
--                             (existing column; its meaning changes, no DDL needed)
--   • business_head_user_id = the same BH, so the BASIS bh-approval gate binds to that
--                             one person instead of the whole business_head pool
--                             (existing column, previously written by nothing)
--   • sales_poc_*           = NEW. The sales point of contact for the request. Defaults
--                             to the signed-in user who raises it and stays EDITABLE.
--                             Mirrors the Customer-Returns header pair (sales_poc /
--                             sales_poc_email, see 070_customer_returns.sql) and adds a
--                             user_id so the address resolves from auth_user rather than
--                             being matched on a display string.
-- Additive + idempotent; existing rows keep the columns NULL and stay valid.
-- Hand-apply via psql (migrate.py does not enumerate samples/):
--   psql "$DATABASE_URL" -f app/db/samples/085_requisition_sales_poc.sql
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS sales_poc_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS sales_poc_name    TEXT,
    ADD COLUMN IF NOT EXISTS sales_poc_email   TEXT;

-- An interim revision of this migration added `poc_name` (a read-only snapshot of the
-- form's author). It was superseded by the editable sales_poc_* trio before release and
-- never carried data, so it is dropped here rather than left as an orphan. Guarded, so
-- this is a no-op on any database that never saw that revision.
ALTER TABLE sample_requisitions
    DROP COLUMN IF EXISTS poc_name;

COMMENT ON COLUMN sample_requisitions.sales_poc_user_id IS
    'Sales point of contact (auth_user). Defaults to the user who raised the requisition; editable.';
COMMENT ON COLUMN sample_requisitions.sales_poc_name IS
    'Display snapshot of the sales POC name — survives the user being renamed or deactivated.';
COMMENT ON COLUMN sample_requisitions.sales_poc_email IS
    'Sales POC address; resolved from auth_user on save, or set directly for a POC without a login.';

COMMENT ON COLUMN sample_requisitions.requestor_user_id IS
    'The business head this requisition is raised FOR (not necessarily its creator — see created_by / sales_poc_user_id).';
COMMENT ON COLUMN sample_requisitions.business_head_user_id IS
    'The BH bound to the BASIS bh-approval gate; set to the selected requestor BH at creation.';
