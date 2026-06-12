-- 066_requisition_pcs_weight.sql
-- Quantity is now derived: pcs × weight_per_piece. Capture both inputs on the
-- sample requisition (and the NPD dev job card it spawns); the existing `quantity`
-- / `target_qty` columns hold the computed total (kg). Additive + idempotent.
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS pcs              NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS weight_per_piece NUMERIC(15,4);

ALTER TABLE npd_dev_job_cards
    ADD COLUMN IF NOT EXISTS pcs              NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS weight_per_piece NUMERIC(15,4);
