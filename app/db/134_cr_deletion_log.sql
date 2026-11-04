-- 134_cr_deletion_log.sql — a record of every deleted customer return.
--
-- A delete removes the return's header, lines and boxes (FK cascade), so until now
-- nothing was left of it: not what it held, not who deleted it. Every delete now
-- writes one row here first, in the same transaction
-- (app/modules/customer_returns/services/create_service.py: _record_deletion).
--
-- snapshot is the return exactly as the detail screen read it (header, lines,
-- boxes, printed box ids included), so a deleted return can be looked up or put back.
--
-- The backend does not need this table to delete: until it exists, the delete goes
-- ahead and the same record is written to the application log instead. Idempotent.

CREATE TABLE IF NOT EXISTS cr_deletion_log (
    id             BIGSERIAL PRIMARY KEY,
    company        TEXT        NOT NULL CHECK (company IN ('CFPL', 'CDPL')),
    rtv_id         TEXT        NOT NULL,
    status         TEXT,                 -- as shown when deleted: Pending / On Hold
    deleted_by     TEXT        NOT NULL,
    deleted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lines_count    INT         NOT NULL DEFAULT 0,
    boxes_count    INT         NOT NULL DEFAULT 0,
    printed_boxes  INT         NOT NULL DEFAULT 0,
    snapshot       JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cr_deletion_log_rtv ON cr_deletion_log (company, rtv_id);
CREATE INDEX IF NOT EXISTS idx_cr_deletion_log_at  ON cr_deletion_log (deleted_at DESC);
