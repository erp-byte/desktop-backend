-- 042_trial_sample_type.sql  (NPD plan §3 Section 3 — Customer Trials)
-- Adds the TRIAL sample type, which mirrors NPD (full BH-approval + gate-pass
-- chain, requisition-bound draft BOM) but carries per-ingredient ownership: the
-- own-only inventory-posting rule (sample_inventory_service) does the rest.
-- Extends the two CHECK constraints and seeds the one approval-matrix row that
-- is sample-type-specific (PRODUCTION_ACK); BH_APPROVAL / INV_MGR_* are already
-- covered by the existing '*' rows, and the notification matrix falls back to '*'.

ALTER TABLE sample_requisitions DROP CONSTRAINT IF EXISTS sample_requisitions_sample_type_check;
ALTER TABLE sample_requisitions ADD CONSTRAINT sample_requisitions_sample_type_check
    CHECK (sample_type IN ('BASIS_RM', 'BASIS_FG', 'NPD', 'INTERNAL', 'TRIAL'));

ALTER TABLE job_card DROP CONSTRAINT IF EXISTS job_card_jobcard_type_check;
ALTER TABLE job_card ADD CONSTRAINT job_card_jobcard_type_check
    CHECK (jobcard_type IN ('REGULAR', 'BASIS_FG_SAMPLE', 'NPD_SAMPLE', 'INTERNAL_FG_OVERRIDE', 'TRIAL_SAMPLE'));

-- The approval-matrix table has its own sample_type CHECK — widen it for TRIAL.
ALTER TABLE sample_approval_role_map DROP CONSTRAINT IF EXISTS sample_approval_role_map_sample_type_check;
ALTER TABLE sample_approval_role_map ADD CONSTRAINT sample_approval_role_map_sample_type_check
    CHECK (sample_type IN ('BASIS_RM', 'BASIS_FG', 'NPD', 'INTERNAL', 'TRIAL', '*'));

INSERT INTO sample_approval_role_map (approval_stage, sample_type, required_role) VALUES
    ('PRODUCTION_ACK', 'TRIAL', 'floor_manager')
ON CONFLICT (approval_stage, sample_type, entity, required_role) DO NOTHING;
