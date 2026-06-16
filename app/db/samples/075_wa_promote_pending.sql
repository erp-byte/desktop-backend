-- 075_wa_promote_pending.sql
-- Inbound-WhatsApp state for the promote-gate REJECT flow. When an approver taps
-- "Reject" (without an inline reason) we record a pending reject keyed by their
-- WhatsApp number + which gate, and ask for the reason; their NEXT message is captured
-- as the reject reason. Kept SEPARATE from wa_pending_action (the NPD review hold flow)
-- so the two inbound flows stay decoupled. Keyed by wa_phone (one pending per phone;
-- a new prompt overwrites). Additive + idempotent.
CREATE TABLE IF NOT EXISTS wa_promote_pending (
    wa_phone       TEXT PRIMARY KEY,                  -- E.164 (no '+') of the approver
    dev_jc_id      BIGINT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    approver_kind  TEXT NOT NULL
                   CHECK (approver_kind IN ('INV_MGR', 'REQUESTOR_BH')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
