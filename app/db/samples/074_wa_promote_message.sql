-- 074_wa_promote_message.sql
-- Maps an outbound promote-approval WhatsApp template message (Meta wamid) back to the
-- dev job card + which gate it was for, so when an approver taps the template's
-- Approve / Reject QUICK-REPLY button we resolve (dev_jc_id, approver_kind) from the
-- button reply's context.id (the wamid of the tapped message). Parallels
-- wa_review_message (067) but for the npd_dev_job_cards promote dual-approval gate.
-- One row per (recipient, sent message). Additive + idempotent.
CREATE TABLE IF NOT EXISTS wa_promote_message (
    wamid          TEXT PRIMARY KEY,                  -- Meta message id of the sent template
    dev_jc_id      BIGINT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    approver_kind  TEXT NOT NULL
                   CHECK (approver_kind IN ('INV_MGR', 'REQUESTOR_BH')),
    wa_phone       TEXT,                              -- recipient approver, E.164 (no '+')
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_wa_promote_message_jc ON wa_promote_message (dev_jc_id);
