-- 079_wa_promote_reminder.sql
-- Resend pacing for the promote-approval WhatsApp gate. One row per (dev job card,
-- gate): notify_promote_review upserts it (resend_count=0, last_sent_at=now) on the
-- initial send, and the in-process promote_reminder_loop bumps last_sent_at +
-- resend_count each time it re-sends a gate still PENDING past the timeout. The
-- pending/answered truth stays in npd_dev_promote_approval.status — this table only
-- decides WHEN to nudge and stops after WHATSAPP_PROMOTE_RESEND_MAX nudges.
-- Applied out-of-band to the live DB (like samples 068-078). Idempotent.
CREATE TABLE IF NOT EXISTS wa_promote_reminder (
    dev_jc_id      BIGINT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    approver_kind  TEXT NOT NULL
                   CHECK (approver_kind IN ('INV_MGR', 'REQUESTOR_BH')),
    last_sent_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resend_count   INT NOT NULL DEFAULT 0,
    PRIMARY KEY (dev_jc_id, approver_kind)
);
