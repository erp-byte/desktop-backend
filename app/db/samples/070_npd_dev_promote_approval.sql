-- 070_npd_dev_promote_approval.sql
-- The two blocking gates for a promote request: INV_MGR (any inventory_manager)
-- and REQUESTOR_BH (the source requisition's requestor). Both must be ACCEPTED
-- before the promote finalizes. id = app-supplied 8-digit BIGINT. Additive + idempotent.
CREATE TABLE IF NOT EXISTS npd_dev_promote_approval (
    id                 BIGINT PRIMARY KEY,
    promote_request_id BIGINT NOT NULL REFERENCES npd_dev_promote_request(id) ON DELETE CASCADE,
    approver_kind      TEXT NOT NULL CHECK (approver_kind IN ('INV_MGR','REQUESTOR_BH')),
    approver_user_id   INT REFERENCES auth_user(user_id),
    status             TEXT NOT NULL DEFAULT 'PENDING'
                       CHECK (status IN ('PENDING','ACCEPTED','REJECTED')),
    remarks            TEXT,
    decided_at         TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_promote_appr_kind
    ON npd_dev_promote_approval (promote_request_id, approver_kind);
CREATE INDEX IF NOT EXISTS ix_promote_appr_req ON npd_dev_promote_approval (promote_request_id);
