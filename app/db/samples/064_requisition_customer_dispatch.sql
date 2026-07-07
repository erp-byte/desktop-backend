-- 064_requisition_customer_dispatch.sql
-- Customer + dispatch-planning fields on the sample requisition, carried onto the
-- NPD development job card it spawns ("attached to the job card part"). Company /
-- customer name are mandatory on the NPD request (enforced at the Pydantic
-- boundary); the rest are optional. Expected dispatch date is set by the BD team
-- (on the request); the confirmed dispatch date is set by NPD (on the job card).
-- Additive + idempotent.
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS company_name             TEXT,
    ADD COLUMN IF NOT EXISTS customer_name            TEXT,
    ADD COLUMN IF NOT EXISTS customer_contact         TEXT,
    ADD COLUMN IF NOT EXISTS customer_ship_to_address TEXT,
    ADD COLUMN IF NOT EXISTS mode_of_transport        TEXT,
    ADD COLUMN IF NOT EXISTS expected_dispatch_date   DATE,
    ADD COLUMN IF NOT EXISTS confirmed_dispatch_date  DATE;

ALTER TABLE npd_dev_job_cards
    ADD COLUMN IF NOT EXISTS company_name             TEXT,
    ADD COLUMN IF NOT EXISTS customer_name            TEXT,
    ADD COLUMN IF NOT EXISTS customer_contact         TEXT,
    ADD COLUMN IF NOT EXISTS customer_ship_to_address TEXT,
    ADD COLUMN IF NOT EXISTS mode_of_transport        TEXT,
    ADD COLUMN IF NOT EXISTS expected_dispatch_date   DATE,
    ADD COLUMN IF NOT EXISTS confirmed_dispatch_date  DATE;
