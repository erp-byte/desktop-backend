-- =========================================================================
-- Migration 041: reconcile unique indexes orphaned from the migration runner
--
-- WHY:
--   scripts/migrate.py applies a hand-curated SQL_FILES list that omits many
--   numbered migrations (009–029, 034, the *_polish twins, …). Every UNIQUE
--   INDEX those files declare via a SEPARATE statement (not inline in CREATE
--   TABLE) can be silently missing on prod — hand-applied installs skip it, or
--   CREATE UNIQUE INDEX fails on pre-existing duplicates and IF NOT EXISTS
--   never retries. 039/040 fixed the two that caused live 500s
--   (uq_jcmc_v2_jc_material, uq_byproducts_jc_cat_mat); this reconciles the
--   rest so they can't bite later.
--
-- WHAT:
--   * idx_bom_override_v2_unique — ON CONFLICT-backed (fulfillment_v2 material
--     breakdown sync). Safe to de-dupe keep-latest (it's an upsert target).
--   * the integrity partial indexes (one-open shift / phase, active weight
--     check, active BOM, live PO) — NOT blindly de-duped: which "open" row
--     would we close? Each is created only when the data is already clean;
--     when conflicts exist it RAISE NOTICEs the count and SKIPS, so this
--     migration never hard-fails and surfaces what needs manual cleanup.
--
--   Every block is to_regclass-guarded on its table AND its index, so the
--   whole file is idempotent and a no-op on databases that don't have the
--   table yet.
--
-- SCOPE: unique indexes only. Table/column-level runner drift (missing tables,
--   ALTERs, backfills from the orphaned migrations) is a separate concern.
-- =========================================================================

BEGIN;

-- -----------------------------------------------------------------------
-- 1. fulfillment_bom_override_v2 — ON CONFLICT (so_fulfillment_id,
--    bom_line_id) WHERE bom_line_id IS NOT NULL  (fulfillment_v2.py:799).
--    Safe to de-dupe: keep the most recent override (largest override_id).
-- -----------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.fulfillment_bom_override_v2') IS NULL THEN
        RAISE NOTICE 'fulfillment_bom_override_v2 absent — skipping idx_bom_override_v2_unique';
        RETURN;
    END IF;

    DELETE FROM fulfillment_bom_override_v2 a
    USING fulfillment_bom_override_v2 b
    WHERE a.so_fulfillment_id = b.so_fulfillment_id
      AND a.bom_line_id       = b.bom_line_id          -- NULLs never match: NULL rows untouched
      AND a.bom_line_id IS NOT NULL
      AND a.override_id < b.override_id;

    CREATE UNIQUE INDEX IF NOT EXISTS idx_bom_override_v2_unique
        ON fulfillment_bom_override_v2(so_fulfillment_id, bom_line_id)
        WHERE bom_line_id IS NOT NULL;
END $$;

-- -----------------------------------------------------------------------
-- 2. job_card_shift_log_v2 — at most one OPEN segment per JC.
-- -----------------------------------------------------------------------
DO $$
DECLARE dup int;
BEGIN
    IF to_regclass('public.job_card_shift_log_v2') IS NULL THEN
        RAISE NOTICE 'job_card_shift_log_v2 absent — skipping uq_jcsl_v2_one_open'; RETURN;
    END IF;
    IF to_regclass('public.uq_jcsl_v2_one_open') IS NOT NULL THEN RETURN; END IF;
    SELECT COUNT(*) INTO dup FROM (
        SELECT job_card_id FROM job_card_shift_log_v2
        WHERE end_at IS NULL GROUP BY job_card_id HAVING COUNT(*) > 1
    ) d;
    IF dup > 0 THEN
        RAISE NOTICE 'uq_jcsl_v2_one_open: % JC(s) with multiple open segments — index NOT created, resolve manually', dup;
        RETURN;
    END IF;
    CREATE UNIQUE INDEX uq_jcsl_v2_one_open
        ON job_card_shift_log_v2(job_card_id) WHERE end_at IS NULL;
END $$;

-- -----------------------------------------------------------------------
-- 3. job_card_weight_check_v2 — one active row per (JC, sample_number).
-- -----------------------------------------------------------------------
DO $$
DECLARE dup int;
BEGIN
    IF to_regclass('public.job_card_weight_check_v2') IS NULL THEN
        RAISE NOTICE 'job_card_weight_check_v2 absent — skipping uq_jcwc_v2_jc_sample_active'; RETURN;
    END IF;
    IF to_regclass('public.uq_jcwc_v2_jc_sample_active') IS NOT NULL THEN RETURN; END IF;
    SELECT COUNT(*) INTO dup FROM (
        SELECT job_card_id, sample_number FROM job_card_weight_check_v2
        WHERE deleted_at IS NULL GROUP BY job_card_id, sample_number HAVING COUNT(*) > 1
    ) d;
    IF dup > 0 THEN
        RAISE NOTICE 'uq_jcwc_v2_jc_sample_active: % conflicting (jc,sample) group(s) — index NOT created, resolve manually', dup;
        RETURN;
    END IF;
    CREATE UNIQUE INDEX uq_jcwc_v2_jc_sample_active
        ON job_card_weight_check_v2(job_card_id, sample_number) WHERE deleted_at IS NULL;
END $$;

-- -----------------------------------------------------------------------
-- 4. job_card_phase_v2 — at most one OPEN phase per JC.
-- -----------------------------------------------------------------------
DO $$
DECLARE dup int;
BEGIN
    IF to_regclass('public.job_card_phase_v2') IS NULL THEN
        RAISE NOTICE 'job_card_phase_v2 absent — skipping uq_jcphase_one_open'; RETURN;
    END IF;
    IF to_regclass('public.uq_jcphase_one_open') IS NOT NULL THEN RETURN; END IF;
    SELECT COUNT(*) INTO dup FROM (
        SELECT job_card_id FROM job_card_phase_v2
        WHERE status = 'open' GROUP BY job_card_id HAVING COUNT(*) > 1
    ) d;
    IF dup > 0 THEN
        RAISE NOTICE 'uq_jcphase_one_open: % JC(s) with multiple open phases — index NOT created, resolve manually', dup;
        RETURN;
    END IF;
    CREATE UNIQUE INDEX uq_jcphase_one_open
        ON job_card_phase_v2(job_card_id) WHERE status = 'open';
END $$;

-- -----------------------------------------------------------------------
-- 5. bom_header — one active BOM per fg_sku_name, and per (fg_sku_name, version).
-- -----------------------------------------------------------------------
DO $$
DECLARE dup int;
BEGIN
    IF to_regclass('public.bom_header') IS NULL THEN
        RAISE NOTICE 'bom_header absent — skipping bom_header unique indexes'; RETURN;
    END IF;

    IF to_regclass('public.uq_bom_header_active_fg') IS NULL THEN
        SELECT COUNT(*) INTO dup FROM (
            SELECT fg_sku_name FROM bom_header
            WHERE is_active = TRUE GROUP BY fg_sku_name HAVING COUNT(*) > 1
        ) d;
        IF dup > 0 THEN
            RAISE NOTICE 'uq_bom_header_active_fg: % fg_sku_name with multiple active BOMs — index NOT created, resolve manually', dup;
        ELSE
            CREATE UNIQUE INDEX uq_bom_header_active_fg
                ON bom_header(fg_sku_name) WHERE is_active = TRUE;
        END IF;
    END IF;

    IF to_regclass('public.uq_bom_header_fg_version') IS NULL THEN
        SELECT COUNT(*) INTO dup FROM (
            SELECT fg_sku_name, version FROM bom_header
            WHERE is_active = TRUE GROUP BY fg_sku_name, version HAVING COUNT(*) > 1
        ) d;
        IF dup > 0 THEN
            RAISE NOTICE 'uq_bom_header_fg_version: % (fg,version) with multiple active BOMs — index NOT created, resolve manually', dup;
        ELSE
            CREATE UNIQUE INDEX uq_bom_header_fg_version
                ON bom_header(fg_sku_name, version) WHERE is_active = TRUE;
        END IF;
    END IF;
END $$;

-- -----------------------------------------------------------------------
-- 6. po_header — one live PO per (entity, po_number).
-- -----------------------------------------------------------------------
DO $$
DECLARE dup int;
BEGIN
    IF to_regclass('public.po_header') IS NULL THEN
        RAISE NOTICE 'po_header absent — skipping uq_po_header_live_entity_pono'; RETURN;
    END IF;
    IF to_regclass('public.uq_po_header_live_entity_pono') IS NOT NULL THEN RETURN; END IF;
    SELECT COUNT(*) INTO dup FROM (
        SELECT entity, po_number FROM po_header
        WHERE deleted_at IS NULL AND po_number IS NOT NULL
        GROUP BY entity, po_number HAVING COUNT(*) > 1
    ) d;
    IF dup > 0 THEN
        RAISE NOTICE 'uq_po_header_live_entity_pono: % live (entity,po_number) dup group(s) — index NOT created, resolve manually', dup;
        RETURN;
    END IF;
    CREATE UNIQUE INDEX uq_po_header_live_entity_pono
        ON po_header(entity, po_number) WHERE deleted_at IS NULL AND po_number IS NOT NULL;
END $$;

COMMIT;

-- ── Verification ─────────────────────────────────────────────────────────
-- All seven should be present (rows missing here = a block hit a conflict and
-- skipped — check the NOTICE output above and resolve the dup data manually).
SELECT indexname
FROM   pg_indexes
WHERE  indexname IN (
           'idx_bom_override_v2_unique',
           'uq_jcsl_v2_one_open',
           'uq_jcwc_v2_jc_sample_active',
           'uq_jcphase_one_open',
           'uq_bom_header_active_fg',
           'uq_bom_header_fg_version',
           'uq_po_header_live_entity_pono'
       )
ORDER  BY indexname;
