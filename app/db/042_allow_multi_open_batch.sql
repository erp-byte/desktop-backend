-- =========================================================================
-- Migration 042: allow concurrent open batches per job card
--
-- WHY:
--   The operator workflow has multiple production lines running the same
--   article in parallel (different shifts, different machines, different
--   floors).  Each line needs its own open batch row on the JC so the
--   per-batch accounting (RM consumption, FG output, off-grade, balance
--   material, additives) can be attributed to the line that produced it.
--
--   Migration 038 (job_card_phase_v2 → job_card_batch_v2 per-record
--   storage) was designed for this — it dropped the "one open per JC"
--   constraint specifically.  Migration 041 (reconcile orphaned unique
--   indexes) RE-INTRODUCED `uq_jcphase_one_open` by mistake, which makes
--   the second POST /batches/open on a JC fail with 'batch_already_open'.
--   This migration reverts that one index without touching the others
--   from 041 (uq_jcsl_v2_one_open, uq_jcwc_v2_jc_sample_active,
--   uq_bom_header_*, uq_po_header_live_entity_pono are all still
--   correct).
--
-- WHAT:
--   DROP INDEX IF EXISTS uq_jcphase_one_open.
--
--   The save path is unaffected — material_consumption_v2 / byproducts_v2 /
--   balance_material_v2 / additive_consumption_v2 all UNIQUE on
--   (job_card_id, COALESCE(batch_id, 0), material_sku_name) so each
--   open batch carries its own per-article row.  The /complete gate at
--   job_card_v2.complete_job_card still refuses if ANY batch is open
--   (closing all opens is a precondition for the JC completing).
-- =========================================================================

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.uq_jcphase_one_open') IS NOT NULL THEN
        EXECUTE 'DROP INDEX uq_jcphase_one_open';
        RAISE NOTICE 'Dropped uq_jcphase_one_open — concurrent open batches per JC are now allowed';
    ELSE
        RAISE NOTICE 'uq_jcphase_one_open absent — no-op';
    END IF;
END $$;

COMMIT;

-- ── Verification ─────────────────────────────────────────────────────────
-- (1) the index should no longer exist:
SELECT indexname
FROM   pg_indexes
WHERE  indexname = 'uq_jcphase_one_open';
-- Expected: zero rows.

-- (2) other 041 indexes should still be present:
SELECT indexname
FROM   pg_indexes
WHERE  indexname IN (
           'idx_bom_override_v2_unique',
           'uq_jcsl_v2_one_open',
           'uq_jcwc_v2_jc_sample_active',
           'uq_bom_header_active_fg',
           'uq_bom_header_fg_version',
           'uq_po_header_live_entity_pono',
           'uq_byproducts_jc_cat_mat'
       )
ORDER  BY indexname;
-- Expected: 7 rows.
