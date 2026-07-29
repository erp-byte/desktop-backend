-- 078_cr_approval_audit.sql
-- Customer-Returns approval flow (email magic-link + in-app decision):
--   * approval audit columns on both company headers — who approved/rejected/held,
--     when, and an optional remark (written by the decision + email-action endpoints).
--   * extra warehouse_cc rows so the BACKEND recipient resolver matches the RAW
--     stored factory_unit (e.g. "D-39") without needing the FE's canonicalization —
--     the Savla cold units are stored as bare D-39 / D-514 codes.
-- Additive + idempotent.

ALTER TABLE cfpl_customer_return_header ADD COLUMN IF NOT EXISTS approved_by     TEXT;
ALTER TABLE cfpl_customer_return_header ADD COLUMN IF NOT EXISTS approved_at     TIMESTAMPTZ;
ALTER TABLE cfpl_customer_return_header ADD COLUMN IF NOT EXISTS approval_remark TEXT;

ALTER TABLE cdpl_customer_return_header ADD COLUMN IF NOT EXISTS approved_by     TEXT;
ALTER TABLE cdpl_customer_return_header ADD COLUMN IF NOT EXISTS approved_at     TIMESTAMPTZ;
ALTER TABLE cdpl_customer_return_header ADD COLUMN IF NOT EXISTS approval_remark TEXT;

-- Bare cold-unit codes for backend warehouse-owner CC matching (raw factory_unit).
INSERT INTO cr_email_routing (kind, match_key, match_type, email, sort_order) VALUES
    ('warehouse_cc', 'd39',  'code', 'vaibhav.kumkar@candorfoods.in', 8),
    ('warehouse_cc', 'd514', 'code', 'vaibhav.kumkar@candorfoods.in', 9)
ON CONFLICT (kind, match_key, email) DO NOTHING;
