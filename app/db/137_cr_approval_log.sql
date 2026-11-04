-- 137_cr_approval_log.sql — what each approval request of a customer return was sent with.
--
-- An approver decides on the figures they were sent: the return's customer, factory
-- unit, invoice and challan, and each line's article, unit, quantity, rate, value and
-- net weight (query_service.figures_version, a short fingerprint of them). Every
-- approval link and WhatsApp card now carries that fingerprint, and a decision is
-- refused when the return's figures no longer match it
-- (app/modules/customer_returns/services/approval_service.py).
--
-- This table records, per return:
--   * 'sent'    each approval request that reached someone, with the fingerprint it
--               carried, who sent it and what went out (outcome);
--   * 'changed' each save that changed the figures, with the new fingerprint.
-- It is how a save knows a return was already sent (and sends it again when its
-- figures change), and how a link or card sent before this change is judged: it
-- carries no fingerprint, so it decides only while nothing is recorded after it.
--
-- wa_return_message.figures holds the fingerprint each WhatsApp card was sent with.
--
-- The backend runs without both: requests are then not recorded (a warning is logged)
-- and cards are stored without a fingerprint, so apply this before the deploy.
-- Idempotent.

CREATE TABLE IF NOT EXISTS cr_approval_log (
    id        BIGSERIAL PRIMARY KEY,
    company   TEXT        NOT NULL CHECK (company IN ('CFPL', 'CDPL')),
    rtv_id    TEXT        NOT NULL,
    event     TEXT        NOT NULL CHECK (event IN ('sent', 'changed')),
    figures   TEXT        NOT NULL,
    actor     TEXT,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome   JSONB
);

CREATE INDEX IF NOT EXISTS idx_cr_approval_log_rtv ON cr_approval_log (company, rtv_id, at DESC);

ALTER TABLE wa_return_message ADD COLUMN IF NOT EXISTS figures TEXT;
