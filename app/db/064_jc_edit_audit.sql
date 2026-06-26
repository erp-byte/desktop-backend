-- 064_jc_edit_audit.sql
-- Audit trail for LIVE (started-chain) Edit-Job-Card actions. The live edit
-- engine mutates job cards mid-run AND writes the qty delta back to the SO
-- (both the so_fulfillment_v2 ledger and so_line) — a sensitive cross-module
-- change — so every action records one row here for traceability.
--
-- Removal itself needs no new columns: it reuses job_card_v2's existing
-- cancelled_snapshot / cancellation_reason / deleted_at / deleted_by (the
-- "record the job-card's data, then cancel" path). The SO ledger side reuses
-- so_revision_log_v2; the so_line writeback reuses log_edit.
--
-- Additive + idempotent. edit_log_id is an app-supplied 8-digit time id minted
-- by the service (new_short_time_id), matching the v2 PK convention.
CREATE TABLE IF NOT EXISTS job_card_edit_log_v2 (
    edit_log_id   BIGINT PRIMARY KEY,
    plan_id       BIGINT,
    plan_line_id  BIGINT,
    job_card_id   BIGINT,                          -- target JC (NULL for add_process)
    action        TEXT NOT NULL CHECK (action IN
                    ('floor_change', 'add_process', 'qty_change', 'remove_process')),
    before_value  JSONB,                           -- prior floor/qty/step, or full JC snapshot on remove
    after_value   JSONB,
    so_sync       JSONB,                           -- {fulfillment_ids, so_line_ids, delta_kg, delta_units}
    reason        TEXT,
    edited_by     TEXT,
    edited_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jc_edit_log_line
    ON job_card_edit_log_v2 (plan_line_id, edited_at DESC);
CREATE INDEX IF NOT EXISTS idx_jc_edit_log_jc
    ON job_card_edit_log_v2 (job_card_id);
