-- ===========================================================================
-- 126_transfer_request_notifications.sql
-- Transfer requests: answering the WhatsApp notices from the message.
--
-- When a request is raised, the store of the supplying site gets a WhatsApp
-- notice with Accept / Hold; once the store accepts, the inventory managers
-- get one with Approve / Hold / Reject (app/modules/transfer/services/request_notify.py).
-- A tap is answered by request_wa_reply.py through the shared WhatsApp webhook.
--
-- transfer_request_wa_message  one row per notice sent with buttons: the
--     message's wamid, the request, the step ('store' / 'manager'), the ERP user
--     it was sent to and their number. A tap normally names its request in the
--     button's payload; a tap that only quotes the notice is matched here.
-- transfer_request_wa_pending  one row per WhatsApp number that tapped Reject or
--     Hold and has been asked for the reason: the request, which decision
--     ('store_reject' / 'reject' / 'store_hold' / 'hold'; 127 widens the check on a
--     database where an older 126 ran), the user, the prompt's wamid (a reply that
--     quotes the prompt is the reason too) and when it was asked. The next text
--     from that number within 30 minutes is the reason; a new tap replaces the
--     row. Kept apart from the sample module's wa_pending_action /
--     wa_promote_pending so the flows cannot take each other's replies.
--
-- request_id is interunit_transfer_requests.id, without a foreign key: the
-- interunit_* tables are managed outside this repo (see 121).
--
-- Not needed before the backend deploys: without these tables the notices still
-- go (email and WhatsApp), the buttons still work by their payloads, and a Hold
-- or Reject tap is answered "please do it in the ERP". Idempotent; one
-- transaction.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS transfer_request_wa_message (
    wamid       text PRIMARY KEY,
    request_id  bigint      NOT NULL,
    stage       text        NOT NULL CHECK (stage IN ('store', 'manager')),
    user_id     integer,
    wa_phone    text        NOT NULL,
    sent_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_transfer_request_wa_message_request
    ON transfer_request_wa_message (request_id);

CREATE TABLE IF NOT EXISTS transfer_request_wa_pending (
    wa_phone      text PRIMARY KEY,
    request_id    bigint      NOT NULL,
    action        text        NOT NULL CHECK (action IN ('store_reject', 'reject', 'store_hold', 'hold')),
    user_id       integer,
    prompt_wamid  text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMIT;
