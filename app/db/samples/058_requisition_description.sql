-- 058_requisition_description — add a free-text `description` column to
-- sample_requisitions for the NPD sample-requisition form (distinct from
-- purpose_tag / purpose_note). Additive + idempotent.

ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS description TEXT;
