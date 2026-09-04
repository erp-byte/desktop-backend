-- 087_sample_dispatch_reminder_log.sql
--- Send-once guard for the NPD dispatch-date reminders.
---
--- The reminder loop ticks hourly and may run on more than one instance, so the
--- decision "has this mail already gone out today?" must not be a read followed by
--- a write — two ticks would both read "not yet". The row IS the claim: the unique
--- index picks one winner per (requisition, kind, day) and only that caller sends.
--- sent_on being part of the key is also what makes the daily chase work — tomorrow
--- is simply a new row, with no separate counter to keep.
---
--- Applied out-of-band like samples 068-086; NOT wired into scripts/migrate.py.
--- Idempotent. Safe to re-run.
---
--- id is an app-supplied 8-digit time-based BIGINT (new_short_time_id +
--- retry-on-collision in dispatch_reminder_service) — the same handle pattern as
--- npd_dev_dispatch.dispatch_id. NOT a SERIAL.
CREATE TABLE IF NOT EXISTS sample_dispatch_reminder_log (
    id              BIGINT PRIMARY KEY,
    requisition_id  BIGINT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    kind            TEXT   NOT NULL,   -- DUE_TOMORROW_NPD | DUE_TOMORROW_OWNER
                                       -- OVERDUE_NPD      | OVERDUE_OWNER
    sent_on         DATE   NOT NULL,   -- the IST day it was sent
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (requisition_id, kind, sent_on)
);
CREATE INDEX IF NOT EXISTS idx_sample_dispatch_reminder_req
    ON sample_dispatch_reminder_log(requisition_id);
