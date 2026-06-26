-- =========================================================================
-- Migration 021: weight-check sample uniqueness
--
-- Migration 020 left job_card_weight_check_v2 without a uniqueness key on
-- (job_card_id, sample_number). A v2 client that fans out 20 samples and
-- fails mid-chain (network drop, validation error, etc.) had no safe way
-- to retry: re-submitting the whole batch would create duplicate rows for
-- the samples that already landed.
--
-- This migration adds a partial unique index so the row count per
-- (JC, sample_number) is capped at one *active* row — soft-deleted rows
-- don't count, so a deleted+re-added flow still works. The service is
-- updated separately to use ON CONFLICT … DO UPDATE so a retry of an
-- already-saved sample becomes idempotent.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- One active sample per (JC, sample_number). soft-deleted rows excluded
-- so the operator can delete + re-add the same sample slot.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jcwc_v2_jc_sample_active
    ON job_card_weight_check_v2(job_card_id, sample_number)
    WHERE deleted_at IS NULL;

COMMIT;
