-- ===========================================================================
-- 116_job_card_bom_change_types.sql
-- job_card_bom_change may hold FG and SFG articles added to a job card (the
-- Material allocation tab's "Use" on other floor stock, and + Add article).
-- They are accounted as RM input (spec Addendum A); their required qty is kg.
-- One transaction, lock_timeout 5s; the guarded block makes a re-run a no-op.
-- MUST follow 115. Idempotent.
-- ===========================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    IF to_regclass('public.job_card_bom_change') IS NULL THEN
        RAISE NOTICE 'job_card_bom_change absent -- apply 115 first';
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.job_card_bom_change'::regclass
           AND conname = 'chk_jcbc_item_type'
           AND pg_get_constraintdef(oid) LIKE '%sfg%'
    ) THEN
        ALTER TABLE job_card_bom_change DROP CONSTRAINT IF EXISTS chk_jcbc_item_type;
        ALTER TABLE job_card_bom_change
            ADD CONSTRAINT chk_jcbc_item_type CHECK (item_type IN ('rm','pm','fg','sfg'));
        ALTER TABLE job_card_bom_change DROP CONSTRAINT IF EXISTS chk_jcbc_required;
        ALTER TABLE job_card_bom_change
            ADD CONSTRAINT chk_jcbc_required CHECK (required_qty IS NULL OR (
                change_type = 'added' AND required_qty > 0
                AND required_unit = CASE item_type WHEN 'pm' THEN 'pcs' ELSE 'kg' END
                AND (item_type <> 'pm' OR required_qty = trunc(required_qty))));
    END IF;
END $$;

COMMIT;
