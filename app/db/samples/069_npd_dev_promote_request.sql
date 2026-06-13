-- 069_npd_dev_promote_request.sql
-- A pending "Record output & promote" on a dev job card. Stashes the operator's
-- close payload (output/accounting + chosen phase) as JSONB so the promote can be
-- finalized later, once both gate approvals land. id is an app-supplied 8-digit
-- BIGINT (new_short_time_id), not a sequence. Additive + idempotent.
CREATE TABLE IF NOT EXISTS npd_dev_promote_request (
    id               BIGINT PRIMARY KEY,
    dev_jc_id        BIGINT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    promote_phase_id BIGINT,
    close_payload    JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','APPROVED','REJECTED','VOID')),
    created_by       INT REFERENCES auth_user(user_id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at       TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_promote_req_live
    ON npd_dev_promote_request (dev_jc_id) WHERE status = 'PENDING';
