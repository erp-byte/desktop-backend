-- 065_wa_pending_action.sql
-- Inbound-WhatsApp state for the NPD review flow. When an NPD reviewer replies
-- "HOLD <req#>" (without an inline reason), we record a pending HOLD keyed by
-- their WhatsApp number and ask for the reason; their NEXT message is captured as
-- the hold reason. Keyed by wa_phone so a new prompt overwrites the prior one
-- (one reviewer is mid-reason for one request at a time). Additive + idempotent.
CREATE TABLE IF NOT EXISTS wa_pending_action (
    wa_phone        TEXT PRIMARY KEY,                 -- E.164 (no '+') of the reviewer
    requisition_id  INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    action          TEXT NOT NULL DEFAULT 'HOLD'
                    CHECK (action IN ('HOLD')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
