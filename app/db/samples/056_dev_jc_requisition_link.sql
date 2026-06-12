-- 056_dev_jc_requisition_link.sql  (soft link: request <-> development job card)
-- When a development job card is created from an approved request's "Develop"
-- button, record the source requisition on the card AND the created card on the
-- requisition, so the request detail/list shows "Open" (to that card) instead of
-- "Develop". The dev-job-card track stays otherwise decoupled — these are plain
-- nullable id columns, no FKs/cascades. Idempotent.
ALTER TABLE npd_dev_job_cards   ADD COLUMN IF NOT EXISTS source_requisition_id BIGINT;
ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS linked_dev_jc_id      BIGINT;
