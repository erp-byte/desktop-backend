-- ===========================================================================
-- 129_po_event_log_columns.sql
-- Give po_event_log the columns the PO audit writer inserts.
--
-- On RDS the table predates 005: it has payload_diff, request_id and a uuid
-- actor_user_id, but no actor_role, reason or payload. Every audit insert from
-- po_commit / po_delete therefore failed. Until the writer took its own
-- savepoint, that failure aborted the PO's transaction, so commits and deletes
-- were silently rolled back. This adds the missing columns and makes
-- actor_user_id TEXT (the ERP writes its numeric user ids; existing uuids are
-- kept as text). Existing columns and rows are left as they are.
--
-- Safe before or after the backend deploys. Idempotent; one transaction.
-- ===========================================================================

BEGIN;

ALTER TABLE po_event_log ADD COLUMN IF NOT EXISTS actor_role TEXT;
ALTER TABLE po_event_log ADD COLUMN IF NOT EXISTS reason     TEXT;
ALTER TABLE po_event_log ADD COLUMN IF NOT EXISTS payload    JSONB;

DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'po_event_log'
           AND column_name = 'actor_user_id') = 'uuid' THEN
        ALTER TABLE po_event_log ALTER COLUMN actor_user_id TYPE TEXT USING actor_user_id::text;
    END IF;
END $$;

COMMIT;
