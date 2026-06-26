-- ============================================================================
-- 034 — Byproducts material attribution
--
-- Problem
-- -------
-- Off-grade / rejection entries on the Output & Accounting tab carry an
-- article (BOM line) dropdown, but the byproducts row that persists them
-- only stored (job_card_id, category, quantity, uom, remarks). The article
-- was collected by the form, sent in the wire payload, and silently
-- discarded. Worse, UNIQUE (job_card_id, category) meant only ONE row per
-- category per JC was retainable — multi-article rejections were
-- impossible by construction.
--
-- Fix
-- ---
-- 1. Add `material_name TEXT NULL` + `bom_line_id INT NULL REFERENCES
--    bom_line(bom_line_id) ON DELETE SET NULL` columns. Both nullable for
--    back-compat (control_sample, pm_* variance, dust, etc. don't attribute
--    to an article).
-- 2. Drop the existing single-key UNIQUE constraint.
-- 3. Add a new UNIQUE that lets multiple rows of the same category coexist
--    as long as they target different articles. `COALESCE(material_name,
--    '')` collapses NULL into a single bucket so the constraint behaves
--    deterministically — without this, NULL-vs-NULL would be treated as
--    distinct by Postgres and an upsert could silently produce duplicates
--    for categories that don't carry an article.
-- ============================================================================

BEGIN;

ALTER TABLE job_card_byproducts_v2
    ADD COLUMN IF NOT EXISTS material_name TEXT,
    ADD COLUMN IF NOT EXISTS bom_line_id   INT
        REFERENCES bom_line(bom_line_id) ON DELETE SET NULL;

-- Drop the old single-key unique. The constraint name follows Postgres'
-- auto-generated convention; the IF EXISTS guards re-runs on fresh DBs
-- where 018 might have produced a different name. The DO block scans
-- pg_constraint for ANY UNIQUE on this exact column tuple and drops it,
-- avoiding hardcoded constraint-name brittleness.
DO $$
DECLARE
    c RECORD;
BEGIN
    FOR c IN
        SELECT con.conname
        FROM   pg_constraint con
        JOIN   pg_class      rel ON rel.oid = con.conrelid
        WHERE  rel.relname = 'job_card_byproducts_v2'
          AND  con.contype = 'u'
          AND  ARRAY(
                 -- pg_attribute.attname is type `name` — cast to text
                 -- so the array comparison against the text[] literal
                 -- below resolves to the standard text-array equality.
                 SELECT attname::text FROM pg_attribute
                 WHERE  attrelid = rel.oid
                   AND  attnum = ANY(con.conkey)
                 ORDER  BY array_position(con.conkey, attnum)
               ) = ARRAY['job_card_id','category']::text[]
    LOOP
        EXECUTE format(
            'ALTER TABLE job_card_byproducts_v2 DROP CONSTRAINT %I',
            c.conname
        );
    END LOOP;
END $$;

-- New uniqueness: (jc, category, material_name) with NULL collapsed to ''.
-- A UNIQUE INDEX is used here (not a constraint) because constraints can't
-- reference expressions in Postgres — this is the canonical workaround.
CREATE UNIQUE INDEX IF NOT EXISTS uq_byproducts_jc_cat_mat
    ON job_card_byproducts_v2 (job_card_id, category, COALESCE(material_name, ''));

CREATE INDEX IF NOT EXISTS idx_jcbp_v2_bom_line
    ON job_card_byproducts_v2(bom_line_id);

COMMIT;
