-- 002_webhooks.sql — Webhook & event delivery infrastructure

CREATE TABLE IF NOT EXISTS webhook_endpoint (
    id              SERIAL PRIMARY KEY,
    entity          TEXT NOT NULL,
    url             TEXT NOT NULL,
    secret          TEXT NOT NULL,
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_subscription (
    id              SERIAL PRIMARY KEY,
    endpoint_id     INT NOT NULL REFERENCES webhook_endpoint(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    filter_jsonb    JSONB DEFAULT '{}',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (endpoint_id, event_type)
);

CREATE TABLE IF NOT EXISTS webhook_delivery (
    id              BIGSERIAL PRIMARY KEY,
    endpoint_id     INT NOT NULL REFERENCES webhook_endpoint(id),
    event_type      TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INT NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    response_code   INT,
    response_body   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delivery_status
    ON webhook_delivery(status) WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_delivery_event
    ON webhook_delivery(event_id);
CREATE INDEX IF NOT EXISTS idx_delivery_endpoint
    ON webhook_delivery(endpoint_id, created_at DESC);

-- HI-06: persist target_roles so a retried delivery can reconstruct the
-- original Event faithfully (and future republish-on-retry paths preserve
-- role-scoped broadcaster routing).
ALTER TABLE webhook_delivery
    ADD COLUMN IF NOT EXISTS target_roles TEXT[] NOT NULL DEFAULT '{}';
