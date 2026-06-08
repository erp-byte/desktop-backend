-- 046_dev_jc_dispatch.sql  (NPD plan §3.2 / Section 2 Step C)
-- The standalone dev job card mints its finished trial sample into R&D on close
-- (Step B). This adds the issue/dispatch path (Step C): a dispatch records who
-- received the developed FG sample and how much, and fires a 265 Goods Issue out
-- of the R&D location. No status change (the card stays CLOSED); the dispatch
-- columns record the event.

ALTER TABLE npd_dev_job_cards
    ADD COLUMN IF NOT EXISTS dispatched_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dispatched_by       INT REFERENCES auth_user(user_id),
    ADD COLUMN IF NOT EXISTS dispatch_recipient  TEXT,
    ADD COLUMN IF NOT EXISTS dispatch_qty        NUMERIC(15, 3),
    ADD COLUMN IF NOT EXISTS dispatch_mat_doc_id TEXT;
