-- 072_requisition_billing_fields.sql
-- Sample-requisition billing checklist: Returnable / Non-returnable / Paid + Amount.
--   • returnable and non_returnable are mutually exclusive (at most one true).
--   • amount is locked to 0 unless paid; when paid it must be > 0 (precision 2).
-- All four default so existing rows stay valid. Hand-apply via psql (migrate.py
-- does not enumerate samples/): psql "$DATABASE_URL" -f app/db/samples/072_requisition_billing_fields.sql
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS returnable     BOOLEAN       NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS non_returnable BOOLEAN       NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS paid           BOOLEAN       NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS amount         NUMERIC(12,2) NOT NULL DEFAULT 0;

-- Idempotent CHECK constraints (Postgres has no ADD CONSTRAINT IF NOT EXISTS).
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_sample_req_return_xor') THEN
        ALTER TABLE sample_requisitions
            ADD CONSTRAINT chk_sample_req_return_xor CHECK (NOT (returnable AND non_returnable));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_sample_req_paid_amount') THEN
        ALTER TABLE sample_requisitions
            ADD CONSTRAINT chk_sample_req_paid_amount CHECK ((paid AND amount > 0) OR (NOT paid AND amount = 0));
    END IF;
END $$;
