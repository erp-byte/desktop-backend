-- =========================================================================
-- Migration 043: capture a full JC snapshot at cancel time
--
-- WHY:
--   cancel_job_card already soft-deletes (status='cancelled' + deleted_at +
--   cancellation_reason), so the row itself is preserved. But the linked
--   tables (consumption_lines, byproducts, balance_materials, additives,
--   shift_log, sign_offs, batch_v2, qc, accounting, ...) can be edited or
--   re-keyed by later admin actions, which makes a stale read of the
--   cancelled JC give a different picture than what the operator actually
--   cancelled.
--
--   `cancelled_snapshot` JSONB captures the FULL detail payload at the
--   moment the cancel ran — operator name + cancellation reason already
--   live on the row's own columns, but the linked-table state did not.
--   Now it does, in one immutable JSONB blob.
--
-- WHAT (idempotent):
--   1. Add cancelled_snapshot JSONB column. Existing rows get NULL.
--   2. Document the shape via comment.
--
--   The service layer (cancel_job_card in services/job_card_v2.py) fills
--   the column inside the same transaction that flips the status — see
--   that file's update query.
-- =========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.job_card_v2') IS NULL THEN
        RAISE NOTICE 'job_card_v2 absent — skipping 043';
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE  table_name = 'job_card_v2' AND column_name = 'cancelled_snapshot'
    ) THEN
        ALTER TABLE job_card_v2
            ADD COLUMN cancelled_snapshot JSONB;
        COMMENT ON COLUMN job_card_v2.cancelled_snapshot IS
            'Full JC + linked-tables snapshot taken at cancel time. '
            'Set by cancel_job_card service in the same txn that flips '
            'status to ''cancelled''. NULL on JCs that were never '
            'cancelled or were cancelled before migration 043.';
    END IF;
END $$;

COMMIT;

-- ── Verification ─────────────────────────────────────────────────────────
SELECT column_name, data_type
FROM   information_schema.columns
WHERE  table_name = 'job_card_v2'
  AND  column_name = 'cancelled_snapshot';
