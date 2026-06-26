-- 071_requisition_email_thread.sql
-- Threaded review-email state on the requisition: the anchor Message-ID (so every
-- later mail replies into one trail), and reminder bookkeeping so the "until accepted"
-- nudges fire on a capped cadence (never a per-second loop). Additive + idempotent.
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS email_thread_msgid TEXT,
    ADD COLUMN IF NOT EXISTS last_reminder_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reminder_count     INT NOT NULL DEFAULT 0;
