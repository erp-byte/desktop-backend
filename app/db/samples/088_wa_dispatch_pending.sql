-- 088_wa_dispatch_pending.sql
--- Inbound-WhatsApp state for the overdue-dispatch chase (npd_dispatch_overdue_team).
---
--- Both of that template's quick-reply buttons are TWO messages deep, which is why this
--- carries a `stage` where wa_pending_action and wa_promote_pending do not:
---
---   Cancel request        -> REASON -> cancel with that reason
---   Change expected date  -> REASON -> DATE -> move the date, reason into the audit trail
---
--- `reason` therefore has to survive between the two prompts of the redate leg; it is the
--- only reason column here, and stays NULL for the whole of stage REASON.
---
--- Keyed by wa_phone like its two siblings: one business head is mid-answer for one
--- requisition at a time, and a fresh tap overwrites whatever they abandoned. Kept SEPARATE
--- from wa_pending_action so the review-hold flow and this one cannot pop each other's rows
--- (wa_pending_action has no stage, and widening its CHECK would couple the two).
---
--- Applied out-of-band like samples 065-087; NOT wired into scripts/migrate.py.
--- Idempotent. Safe to re-run.
CREATE TABLE IF NOT EXISTS wa_dispatch_pending (
    wa_phone        TEXT PRIMARY KEY,                  -- E.164 (no '+') of the business head
    requisition_id  BIGINT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    action          TEXT NOT NULL CHECK (action IN ('REDATE', 'CANCEL')),
    stage           TEXT NOT NULL CHECK (stage IN ('REASON', 'DATE')),
    reason          TEXT,                              -- captured at stage REASON, used at DATE
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_wa_dispatch_pending_req
    ON wa_dispatch_pending (requisition_id);
