-- ===========================================================================
-- 122_transfer_reason_optional.sql
-- Transfers: the header's reason_code becomes optional.
--
-- A dispatch made from a transfer request (Scan & send) no longer asks for a
-- reason: once the inventory manager has approved the request, its route,
-- items, quantities and reason are locked, and the request's reason text is
-- stored in remark. The backend writes reason_code = NULL for such a dispatch
-- (create and edit). A transfer without a request may still carry a reason
-- code; it is optional there too, and a blank one is stored as NULL.
--
-- interunit_transfers_header is managed outside this repo, like the other
-- interunit_* tables, so the ALTER is guarded with to_regclass (as in 121): on
-- a database without the table this file does nothing. DROP NOT NULL on a
-- column that is already nullable is a no-op.
--
-- MUST run BEFORE the backend that writes NULL is deployed: every dispatch
-- from a request fails the NOT NULL constraint until it has.
-- Idempotent; one transaction.
-- ===========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.interunit_transfers_header') IS NOT NULL THEN
        ALTER TABLE public.interunit_transfers_header
            ALTER COLUMN reason_code DROP NOT NULL;

        COMMENT ON COLUMN public.interunit_transfers_header.reason_code IS
            'Optional reason code. NULL for a dispatch from a transfer request: the request''s reason text is in remark.';
    END IF;
END
$$;

COMMIT;
