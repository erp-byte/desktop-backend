-- =========================================================================
-- Migration 032: B11 polish - maker-checker schema hardening
--
-- Pulls in the schema-side fixes from the B11 review:
--
--   C2(d) UNIQUE partial index on bom_header(fg_sku_name) WHERE is_active
--         guarantees there is at most one active BOM per FG SKU. Pairs with
--         the _apply_permanent_bom change that flips the old row to
--         is_active=FALSE inside the same txn as inserting the new version.
--
--   C3    Non-partial UNIQUE on bom_header(fg_sku_name, version) is the
--         defense-in-depth backstop for the advisory-lock fix in
--         _apply_permanent_bom. Two concurrent permanent-BOM amendments on
--         the same fg_sku will now hit either the advisory lock (serializing
--         them) or this UNIQUE (aborting the second txn).
--
--   H5    checker1_note / checker2_note TEXT columns on
--         bom_amendment_request_v2 so the maker-checker UI's note field
--         actually persists (router accepts `note`, service was dropping it
--         silently before B11 polish).
--
--   H7    consumed_at TIMESTAMPTZ on bom_amendment_request_v2 - the audit
--         token columns that B5 force-close (and future
--         consumed_at_complete_time consumers) flip when they redeem an
--         approved override. Without this column an approved
--         unbalanced_close_override could be re-used indefinitely.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- -----------------------------------------------------------------
-- C2(d) - at most one active BOM per fg_sku_name. The maker-checker
--        apply step now flips is_active=FALSE on the previous row
--        before inserting the new version row, so the partial
--        uniqueness holds across the txn boundary.
-- -----------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_bom_header_active_fg
    ON bom_header(fg_sku_name)
    WHERE is_active = TRUE;

-- -----------------------------------------------------------------
-- C3    - defense-in-depth uniqueness on (fg_sku_name, version).
--         _apply_permanent_bom acquires a pg_advisory_xact_lock on
--         hashtext(fg_sku_name) before reading current.version, but
--         if two concurrent amendments slip past the lock (e.g.
--         locks released across a connection swap), this UNIQUE
--         aborts the second commit instead of letting two rows
--         share the same (fg, version) pair.
--
--         PARTIAL on is_active=TRUE so historical inactive duplicates
--         (legacy bulk re-imports that pre-date this gate) don't block
--         the migration. The advisory lock + the active-only partial
--         is sufficient to prevent future duplicates - inactive rows
--         can't be redeemed anyway.
-- -----------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_bom_header_fg_version
    ON bom_header(fg_sku_name, version)
    WHERE is_active = TRUE;

-- -----------------------------------------------------------------
-- H5    - persist the checker note. Routed into checker1_note on the
--         pending_review -> {pending_final | approved} step, and into
--         checker2_note on the pending_final -> approved step.
-- -----------------------------------------------------------------
ALTER TABLE bom_amendment_request_v2
    ADD COLUMN IF NOT EXISTS checker1_note TEXT,
    ADD COLUMN IF NOT EXISTS checker2_note TEXT;

-- -----------------------------------------------------------------
-- H7    - consumed_at audit-token timestamp. B11 only adds the
--         column; the B5 force-close consumer (and future
--         consumed_at_complete_time / consumed_at_stop_time /
--         consumed_at_force_unlock_time / consumed_at_byproducts_save
--         consumers) will atomically UPDATE consumed_at = NOW()
--         alongside their own state flip so an approved noop
--         amendment cannot be redeemed twice.
-- -----------------------------------------------------------------
ALTER TABLE bom_amendment_request_v2
    ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;

COMMENT ON COLUMN bom_amendment_request_v2.consumed_at IS
    'Set by the downstream consumer (B5 force-close, stop_process, '
    'force_unlock, ega_override byproducts-save, unbalanced_close_override '
    'JC /complete) when an approved noop-apply amendment is redeemed. '
    'NULL means the token is still available for redemption.';

COMMIT;
