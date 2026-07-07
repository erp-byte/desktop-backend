-- 061_dev_jc_phases.sql
-- Phase-wise tracking for development job cards. An NPD trial runs over many days
-- in operator-defined phases (e.g. "Trial batch 1", "Sensory evaluation",
-- "Reformulation") — each is started and completed independently, mirroring the
-- production job_card_process_step (PENDING -> IN_PROGRESS -> COMPLETED with
-- start/complete timestamps). Phases run inside the card's IN_DEVELOPMENT state;
-- the card lifecycle (DRAFT -> IN_DEVELOPMENT -> CLOSED) is unchanged. Idempotent.

-- phase_id is an app-supplied 8-digit time-based BIGINT (new_short_time_id), the
-- same handle pattern as the job-card id — NOT a SERIAL.
CREATE TABLE IF NOT EXISTS npd_dev_job_card_phases (
    phase_id        BIGINT PRIMARY KEY,
    dev_jc_id       BIGINT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    phase_number    INT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED')),
    started_at      TIMESTAMPTZ,
    started_by      INT REFERENCES auth_user(user_id),
    completed_at    TIMESTAMPTZ,
    completed_by    INT REFERENCES auth_user(user_id),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (dev_jc_id, phase_number)
);
-- If an earlier run created phase_id as SERIAL, convert it to the app-supplied
-- BIGINT (drop the sequence default, widen). No-ops when already correct.
ALTER TABLE npd_dev_job_card_phases ALTER COLUMN phase_id DROP DEFAULT;
ALTER TABLE npd_dev_job_card_phases ALTER COLUMN phase_id TYPE BIGINT;
CREATE INDEX IF NOT EXISTS idx_npd_dev_jc_phases ON npd_dev_job_card_phases(dev_jc_id);
