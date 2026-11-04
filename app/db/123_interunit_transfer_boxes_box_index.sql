-- ===========================================================================
-- 123_interunit_transfer_boxes_box_index.sql
-- Transfers: an index on interunit_transfer_boxes (box_id, transaction_no).
--
-- The transfer module is record only: a dispatch copies its boxes and leaves
-- their stock rows where they are, so a re-use guard keeps a box on one
-- transfer at a time. The guard reads interunit_transfer_boxes by box_id on
-- every box scan (the three box lookups) and on every POST / PUT
-- /api/v1/transfer/transfers, and the cold-stock box pick skips the boxes it
-- finds there by box_id and transaction_no. Without an index each of those
-- reads scans the whole table, which holds every box ever transferred.
--
-- interunit_transfer_boxes is managed outside this repo, like the other
-- interunit_* tables, so the CREATE INDEX is guarded with to_regclass (as in
-- 121 / 122): on a database without the table this file does nothing. An
-- index of the same name that already exists is kept as it is.
--
-- Plain CREATE INDEX (CONCURRENTLY cannot run in a transaction): writes to the
-- table wait while the index builds, so run it outside dispatch hours.
-- Not needed before the backend deploys (the reads work without it, only
-- slower). Idempotent; one transaction.
-- ===========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.interunit_transfer_boxes') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS ix_interunit_transfer_boxes_box_id_tno
            ON public.interunit_transfer_boxes (box_id, transaction_no);
    END IF;
END
$$;

COMMIT;
