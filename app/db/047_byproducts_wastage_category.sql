-- =========================================================================
-- Migration 047: add 'wastage' to job_card_byproducts_v2.category CHECK list.
--
-- WHY:
--   The JC Output & Accounting form's Off-Grade dropdown
--   (web_replica/src/app/modules/job-card/[id]/page.tsx:478) exposes
--   "Wastage" as a category — the comment there claims wastage is
--   already a server-side bucket. It isn't:
--     - The Python service VALID_BP_CATEGORIES (jc_accounting_v2.py:529)
--       lists tukda / damaged / black_stained / without_shell /
--       empty_shells / dust / balance_material / rejection /
--       control_sample / other / pm_*  — NO wastage.
--     - The DB CHECK constraint declared in 018_jc_accounting_v2.sql:90
--       matches the same set, also without wastage.
--
--   When the operator picks Wastage, the frontend POSTs the byproduct
--   row with category="wastage", the service returns
--   {"error": "invalid_category"}, and the whole save 400s — losing the
--   operator's consumption + off-grade typing along with it.
--
--   Wastage is the right semantic name for "raw material that was
--   discarded with traceability" — distinct from rejection (off-grade
--   FG) and balance_material (returned RM). Adding it as a first-class
--   category lets save_byproducts persist a per-material wastage row
--   alongside the accounting summary's wastage_qty roll-up (already
--   populated by the frontend).
--
-- WHAT (idempotent):
--   Drop the CHECK constraint by name (located dynamically since 018's
--   declaration relies on PG to auto-name it), then re-add it with the
--   extended category list. No data migration required — no existing
--   rows have category='wastage' because the CHECK currently rejects
--   them.
-- =========================================================================

BEGIN;

DO $$
DECLARE
    cn TEXT;
BEGIN
    IF to_regclass('public.job_card_byproducts_v2') IS NULL THEN
        RAISE NOTICE 'job_card_byproducts_v2 absent — skipping wastage category';
        RETURN;
    END IF;

    -- Locate the category CHECK constraint by its key column. PG
    -- auto-names it (job_card_byproducts_v2_category_check or similar);
    -- locating by attnum keeps this resilient to whatever name PG
    -- assigned in this environment.
    SELECT con.conname INTO cn
    FROM   pg_constraint con
    JOIN   pg_class      rel ON rel.oid = con.conrelid
    JOIN   pg_attribute  att ON att.attrelid = rel.oid AND att.attname = 'category'
    WHERE  rel.relname = 'job_card_byproducts_v2'
      AND  con.contype = 'c'
      AND  array_length(con.conkey, 1) = 1
      AND  con.conkey[1] = att.attnum
    LIMIT 1;

    IF cn IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE job_card_byproducts_v2 DROP CONSTRAINT %I',
            cn
        );
    END IF;

    -- Re-add with 'wastage' included. PM-variance categories were added
    -- by migration 028; keep them in scope so we don't accidentally
    -- narrow the allow list.
    ALTER TABLE job_card_byproducts_v2
        ADD CONSTRAINT job_card_byproducts_v2_category_check
        CHECK (category IN (
            'tukda',
            'damaged',
            'black_stained',
            'without_shell',
            'empty_shells',
            'dust',
            'balance_material',
            'rejection',
            'control_sample',
            'wastage',
            'other',
            'pm_torn',
            'pm_damaged',
            'pm_misprint',
            'pm_rejection',
            'pm_wasted'
        ));
END $$;

COMMIT;

-- ── Verification ──────────────────────────────────────────────────────
-- Confirm 'wastage' is now a permitted category.
SELECT pg_get_constraintdef(con.oid) AS check_def
FROM   pg_constraint con
JOIN   pg_class      rel ON rel.oid = con.conrelid
WHERE  rel.relname = 'job_card_byproducts_v2'
  AND  con.contype = 'c'
  AND  pg_get_constraintdef(con.oid) ILIKE '%category%';
