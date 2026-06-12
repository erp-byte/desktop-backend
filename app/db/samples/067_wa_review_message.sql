-- 067_wa_review_message.sql
-- Maps an outbound NPD review / updated template message (Meta wamid) back to the
-- requisition it was about, so when an NPD reviewer taps the template's Accept /
-- Hold QUICK-REPLY button we can resolve the request from the button reply's
-- context.id (the wamid of the message they tapped). Quick-reply buttons carry a
-- static payload, so the wamid is the only reliable link from tap -> request.
--
-- One row per (recipient, sent template message). Superseded silently on a wamid
-- conflict (the same message id never legitimately recurs). Additive + idempotent.
CREATE TABLE IF NOT EXISTS wa_review_message (
    wamid           TEXT PRIMARY KEY,                 -- Meta message id of the sent template
    requisition_id  INT  NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'REVIEW'
                    CHECK (kind IN ('REVIEW', 'UPDATED')),
    wa_phone        TEXT,                             -- recipient reviewer, E.164 (no '+')
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_wa_review_message_req ON wa_review_message (requisition_id);
