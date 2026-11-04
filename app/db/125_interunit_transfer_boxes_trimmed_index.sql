-- ===========================================================================
-- 125_interunit_transfer_boxes_trimmed_index.sql
-- Transfers: an index on interunit_transfer_boxes (btrim(box_id),
-- btrim(transaction_no)).
--
-- Where a box is (R3) is decided by its transfer history: every box scan (the
-- three box lookups), every POST / PUT /api/v1/transfer/transfers, every Scan &
-- send save and the cold box pick read interunit_transfer_boxes for the boxes
-- they judge. The ERP stores box_id / transaction_no trimmed, but IMS stored
-- them as they were sent, so a row with a stray space was invisible to that
-- read and the box was judged by an older transfer, or as free. The read now
-- matches btrim(box_id); this index serves it (123's plain (box_id,
-- transaction_no) index does not serve an expression).
--
-- interunit_transfer_boxes is managed outside this repo, like the other
-- interunit_* tables, so the CREATE INDEX is guarded with to_regclass (as in
-- 121 - 123): on a database without the table this file does nothing. An
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
        CREATE INDEX IF NOT EXISTS ix_interunit_transfer_boxes_btrim_box_id_tno
            ON public.interunit_transfer_boxes (btrim(box_id), btrim(transaction_no));
    END IF;
END
$$;

COMMIT;
