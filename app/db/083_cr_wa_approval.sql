-- 083_cr_wa_approval.sql
-- Customer-Returns head-approval over WhatsApp: maps the wamid of a sent
-- `customer_returns_head_approval` template message back to its CR, so an inbound
-- Approve/Reject/Hold button tap (quoted with context.id = that wamid) resolves to
-- the right return. Mirrors the NPD wa_review_message / wa_promote_message tables.
-- The company is stored so the decision hits the right CFPL/CDPL table. Additive/idempotent.
CREATE TABLE IF NOT EXISTS wa_return_message (
    wamid       TEXT PRIMARY KEY,            -- Meta message id of the sent template
    company     TEXT NOT NULL,               -- CFPL | CDPL
    rtv_id      TEXT NOT NULL,               -- the CR- id the buttons act on
    wa_phone    TEXT,                        -- recipient BH, E.164 (no '+')
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_return_message_rtv ON wa_return_message(company, rtv_id);
