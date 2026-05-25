-- =========================================================================
-- 010_so_line_time_id.sql
-- Change so_line.so_line_id from SERIAL to application-supplied BIGINT.
-- The application now generates an 8-digit time-based short ID via
-- app.core.helpers.new_short_time_id() at insertion time, matching the
-- pattern used by so_fulfillment_v2.so_fulfillment_id.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

-- 1) Widen so_line.so_line_id to BIGINT (no-op if already BIGINT)
ALTER TABLE so_line ALTER COLUMN so_line_id TYPE BIGINT;

-- 2) Drop the SERIAL sequence default — app supplies the value now
ALTER TABLE so_line ALTER COLUMN so_line_id DROP DEFAULT;

-- 3) Widen FK columns that reference so_line.so_line_id, for type symmetry.
--    Postgres permits cross-integer-family FKs, but matching types prevents
--    surprises in joins, ON CONFLICT specs, and array casts.
ALTER TABLE so_gst_reconciliation ALTER COLUMN so_line_id TYPE BIGINT;
ALTER TABLE so_fulfillment        ALTER COLUMN so_line_id TYPE BIGINT;

-- 4) Drop the now-unused sequence (idempotent)
DROP SEQUENCE IF EXISTS so_line_so_line_id_seq;
