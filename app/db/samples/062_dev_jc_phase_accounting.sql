-- 062_dev_jc_phase_accounting.sql
-- Per-phase output + material accounting on a development job card. Each trial
-- phase consumes RM and yields output, so the accounting (the same shape as the
-- card-level close) is captured when the phase is COMPLETED and shown per phase.
-- Additive + idempotent.
ALTER TABLE npd_dev_job_card_phases
    ADD COLUMN IF NOT EXISTS output_qty          NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS output_uom          TEXT,
    ADD COLUMN IF NOT EXISTS rm_consumed_qty     NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS wastage_qty         NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS extra_give_away_qty NUMERIC(15,3),
    ADD COLUMN IF NOT EXISTS yield_pct           NUMERIC(8,3);
