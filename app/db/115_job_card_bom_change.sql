-- ===========================================================================
-- 115_job_card_bom_change.sql
-- Per-job-card BOM changes. Spec:
-- docs/superpowers/specs/2026-09-21-job-card-bom-changes-design.md
--
-- 1. job_card_bom_change: an RM/PM article removed from, or added to, ONE job
--    card's BOM -- every stage card of its chain. scope_job_card_id is the
--    chain's first card (walk prev_job_card_id back while the plan line stays
--    the same). The BOM module's bom_line is never written. One live change per
--    article per job card; undoing keeps the row as history.
-- 2. Returns rows become unique per ARTICLE, not per missing BOM line:
--    uq_jcbm_v2_jc_batch_line_type replaces uq_jcbm_v2_jc_batch_bom_type and adds
--    UPPER(BTRIM(material_name)) for rows with no bom_line_id, so two added
--    articles can each have a 'returned' row in one batch. The new key is the old
--    one plus a column, so it cannot fail on data the old index accepted.
--
-- DEPLOY ORDER: deploy the server that understands this FIRST, then apply 115.
-- The old server's accounting record screen names the old index in ON CONFLICT
-- and fails once it is dropped. 044 was edited to stop re-creating the old index
-- once the new one exists (the runner re-runs every file on every deploy).
--
-- LOCKS: the whole file is one transaction with lock_timeout 5s. The swap takes
-- ACCESS EXCLUSIVE on job_card_balance_material_v2 first (no lock upgrade, no
-- deadlock) and only while the old index still exists, so a re-run takes no lock
-- on that table. CONCURRENTLY is impossible under this runner (see 092).
--
-- MUST follow 044, 092 and 111. Idempotent.
-- ===========================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS job_card_bom_change (
    change_id               BIGINT PRIMARY KEY,
    scope_job_card_id       BIGINT NOT NULL,
    plan_line_id            BIGINT NOT NULL REFERENCES production_plan_line_v2 (plan_line_id) ON DELETE CASCADE,
    change_type             TEXT   NOT NULL,
    material_sku_name       TEXT   NOT NULL,
    item_type               TEXT   NOT NULL,
    sku_id                  INT,
    required_qty            NUMERIC(15,3),
    required_unit           TEXT,
    note                    TEXT,
    made_on_job_card_id     BIGINT NOT NULL,
    made_on_job_card_number TEXT   NOT NULL,
    changed_by              TEXT   NOT NULL,
    changed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    undone_by               TEXT,
    undone_at               TIMESTAMPTZ,
    undo_reason             TEXT,
    CONSTRAINT chk_jcbc_type          CHECK (change_type IN ('removed','added')),
    CONSTRAINT chk_jcbc_item_type     CHECK (item_type IN ('rm','pm')),
    CONSTRAINT chk_jcbc_required_pair CHECK ((required_qty IS NULL) = (required_unit IS NULL)),
    CONSTRAINT chk_jcbc_required      CHECK (required_qty IS NULL OR (
        change_type = 'added' AND required_qty > 0
        AND required_unit = CASE item_type WHEN 'rm' THEN 'kg' ELSE 'pcs' END
        AND (item_type = 'rm' OR required_qty = trunc(required_qty)))),
    CONSTRAINT chk_jcbc_sku           CHECK ((change_type = 'added') = (sku_id IS NOT NULL)),
    CONSTRAINT chk_jcbc_undone        CHECK ((undone_at IS NULL) = (undone_by IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_job_card_bom_change_live
    ON job_card_bom_change (scope_job_card_id, UPPER(BTRIM(material_sku_name)))
    WHERE undone_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_job_card_bom_change_line
    ON job_card_bom_change (plan_line_id);

DO $$
BEGIN
    IF to_regclass('public.uq_jcbm_v2_jc_batch_bom_type') IS NOT NULL THEN
        LOCK TABLE job_card_balance_material_v2 IN ACCESS EXCLUSIVE MODE;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_line_type
            ON job_card_balance_material_v2
               (job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0),
                (CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END),
                balance_type);
        DROP INDEX uq_jcbm_v2_jc_batch_bom_type;
    ELSIF to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NULL THEN
        CREATE UNIQUE INDEX uq_jcbm_v2_jc_batch_line_type
            ON job_card_balance_material_v2
               (job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0),
                (CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END),
                balance_type);
    END IF;
END $$;

COMMIT;
