-- 068_drop_requisition_number.sql
-- Retire the human SMP-YYYYMMDD-NNNN requisition_number entirely. The 8-digit
-- app-supplied request_id (BIGINT, the PRIMARY KEY since migration 057) is now the
-- sole surfaced identifier — every service, notification, WhatsApp message and
-- frontend surface references request_id. Nothing reads requisition_number any more.
--
-- Dropping the column also drops its UNIQUE constraint/index. The seq_sample_req
-- sequence that fed the SMP counter (migration 037) becomes orphaned, so retire it
-- too. No FK ever referenced requisition_number (inbound FKs target id/request_id),
-- so the drop has no cascade beyond the column's own constraint. Additive +
-- idempotent: safe to re-run.
--
-- NOTE: deploy the code that stops INSERTing requisition_number BEFORE running this,
-- or run them together — an old build still listing the column in its INSERT would
-- fail once the column is gone.
ALTER TABLE sample_requisitions DROP COLUMN IF EXISTS requisition_number;

DROP SEQUENCE IF EXISTS seq_sample_req;
