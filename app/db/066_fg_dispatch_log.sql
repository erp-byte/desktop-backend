-- 066_fg_dispatch_log.sql
-- Audit log for FG dispatch notifications raised from a completed packaging
-- stage on the Plan List ("Dispatch to" button). One row per dispatch (per
-- article + packaging batch), capturing the emailed body fields, the
-- operator-entered transport details, and the resolved To/CC recipients.
-- Additive + idempotent. dispatch_id is an app-supplied 8-digit time id.
CREATE TABLE IF NOT EXISTS fg_dispatch_log_v2 (
    dispatch_id        BIGINT PRIMARY KEY,
    plan_id            BIGINT,
    plan_line_id       BIGINT,
    job_card_id        BIGINT,                          -- the packaging (FG) job card
    job_card_number    TEXT,
    batch_id           BIGINT,
    batch_number       INT,                             -- "phase number"
    fg_sku_name        TEXT,
    qty_kg             NUMERIC(15,3),
    qty_units          NUMERIC(15,3),
    num_boxes          INT,
    warehouse          TEXT,
    floor              TEXT,
    customer_name      TEXT,
    customer_location  TEXT,
    vehicle_number     TEXT,
    transporter        TEXT,
    transport_location TEXT,
    to_emails          TEXT[],
    cc_emails          TEXT[],
    subject            TEXT,
    body               TEXT,
    email_sent         BOOLEAN DEFAULT FALSE,
    dispatched_by      TEXT,
    dispatched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fg_dispatch_line ON fg_dispatch_log_v2 (plan_line_id, dispatched_at DESC);
CREATE INDEX IF NOT EXISTS idx_fg_dispatch_jc   ON fg_dispatch_log_v2 (job_card_id);
