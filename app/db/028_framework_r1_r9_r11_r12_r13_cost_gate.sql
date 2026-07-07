-- =========================================================================
-- Migration 028: Framework R-blocks (R1/R2/R8/R9/R11/R12) + cost gate
--                + role rename (stores_manager -> inventory_manager)
--                + background consumption variance capture
--
-- Lands the schema scaffolding for:
--   R2  - plan-line qty kg override flags
--   R8  - maker-checker amendment request ledger + JC exception child
--   R9  - per-BOM closure tolerance + invisible-loss column
--   R11 - PM over-consumption category enum extension + JSONB roll-up
--   R12 - QC notification dispatch log
--
--   Cost-metric access gate (B13): no schema change - enforced at API layer.
--   Background variance capture (B12): job_card_consumption_variance_v2.
--   Role rename (B0): stores_manager -> inventory_manager (permissions
--                     follow via role_id FK; no permission-row migration).
--   New role: production_manager (R8 rows 7/8/12 Checker 2).
--
--   Variance approval row (R8 row 3) is REMOVED from the initial scope -
--   variance is captured silently into job_card_consumption_variance_v2
--   for the future commercial / inventory-costing workstream.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- 1. R9 - per-BOM closure tolerance
ALTER TABLE bom_header
    ADD COLUMN IF NOT EXISTS allowed_balance_tolerance_pct NUMERIC(5,4) NOT NULL DEFAULT 0.001;

COMMENT ON COLUMN bom_header.allowed_balance_tolerance_pct IS
    'R9 closure-gate tolerance: abs(balance_diff) / total_input_qty must be <= this to allow PUT job-cards-v2 id complete. Default 0.001 (0.1 pct).';

-- 2. R2 - plan-line kg override flags
ALTER TABLE production_plan_line_v2
    ADD COLUMN IF NOT EXISTS kg_was_overridden BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE production_plan_line_v2
    ADD COLUMN IF NOT EXISTS override_reason TEXT;

-- 3. R11 - extend job_card_byproducts_v2.category enum
--    Original constraint is unnamed (inline). Discover it, drop it, re-add.
DO $$
DECLARE
    v_constraint_name TEXT;
BEGIN
    SELECT conname INTO v_constraint_name
    FROM   pg_constraint
    WHERE  conrelid = 'job_card_byproducts_v2'::regclass
      AND  contype  = 'c'
      AND  pg_get_constraintdef(oid) LIKE '%category%';

    IF v_constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE job_card_byproducts_v2 DROP CONSTRAINT %I', v_constraint_name);
    END IF;

    ALTER TABLE job_card_byproducts_v2
        ADD CONSTRAINT job_card_byproducts_v2_category_check
        CHECK (category IN (
            'tukda', 'damaged', 'black_stained', 'without_shell',
            'empty_shells', 'dust', 'balance_material', 'rejection',
            'control_sample', 'other',
            'pm_torn', 'pm_damaged', 'pm_misprint', 'pm_rejection', 'pm_wasted'
        ));
END $$;

-- 4. R9 / R11 - job_card_accounting_v2 column adds
ALTER TABLE job_card_accounting_v2
    ADD COLUMN IF NOT EXISTS invisible_loss_pct NUMERIC(6,3);
ALTER TABLE job_card_accounting_v2
    ADD COLUMN IF NOT EXISTS pm_variance_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN job_card_accounting_v2.invisible_loss_pct IS
    'R9 process_loss_pct + ega_loss_pct. Persisted so dashboards can index without re-summing.';

COMMENT ON COLUMN job_card_accounting_v2.pm_variance_breakdown IS
    'R11 PM variance roll-up. Shape: pm_torn pcs, pm_damaged pcs, etc. JSONB so future PM categories add without migration.';

-- 5. R8 - bom_amendment_request_v2 (maker-checker request ledger)
CREATE TABLE IF NOT EXISTS bom_amendment_request_v2 (
    request_id        BIGINT PRIMARY KEY,
    request_type      TEXT NOT NULL CHECK (request_type IN (
        'one_off_material_add',
        'one_off_material_remove',
        'permanent_bom_add',
        'permanent_bom_remove',
        'permanent_bom_qty_change',
        'plan_delete',
        'stop_process',
        'force_unlock',
        'uom_correction',
        'ncr_disposition',
        'unbalanced_close_override',
        'ega_override'
    )),
    bom_id            INT REFERENCES bom_header(bom_id),
    job_card_id       BIGINT REFERENCES job_card_v2(job_card_id),
    payload_jsonb     JSONB NOT NULL,
    maker_user_id     INT REFERENCES auth_user(user_id),
    checker1_user_id  INT REFERENCES auth_user(user_id),
    checker2_user_id  INT REFERENCES auth_user(user_id),
    status            TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'pending_review', 'pending_final',
        'approved', 'applied', 'rejected', 'withdrawn'
    )),
    reason            TEXT,
    rejection_reason  TEXT,
    applied_version   INT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at        TIMESTAMPTZ,
    applied_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bom_amend_status  ON bom_amendment_request_v2(status);
CREATE INDEX IF NOT EXISTS idx_bom_amend_type    ON bom_amendment_request_v2(request_type);
CREATE INDEX IF NOT EXISTS idx_bom_amend_jc      ON bom_amendment_request_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_bom_amend_bom     ON bom_amendment_request_v2(bom_id);
CREATE INDEX IF NOT EXISTS idx_bom_amend_maker   ON bom_amendment_request_v2(maker_user_id);
CREATE INDEX IF NOT EXISTS idx_bom_amend_pending ON bom_amendment_request_v2(status)
    WHERE status IN ('pending_review', 'pending_final');

-- 6. R8 - jc_material_exception_v2 (JC-side audit child)
CREATE TABLE IF NOT EXISTS jc_material_exception_v2 (
    exception_id      BIGINT PRIMARY KEY,
    job_card_id       BIGINT NOT NULL REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    request_id        BIGINT NOT NULL REFERENCES bom_amendment_request_v2(request_id),
    material_sku_name TEXT NOT NULL,
    qty_kg            NUMERIC(15,3),
    qty_pcs           NUMERIC(15,3),
    exception_type    TEXT NOT NULL CHECK (exception_type IN ('one_off_add', 'one_off_remove')),
    reason            TEXT,
    status            TEXT NOT NULL DEFAULT 'applied',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jcex_jc      ON jc_material_exception_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcex_request ON jc_material_exception_v2(request_id);

-- 7. NEW - job_card_consumption_variance_v2 (silent BOM vs actual capture)
--    Cost columns left NULL - populated later by the inventory-ledger
--    costing workstream. Snapshots bom_version + bom_prescribed_qty so
--    future BOM amendments do not retroactively shift historical reporting.
CREATE TABLE IF NOT EXISTS job_card_consumption_variance_v2 (
    variance_id              BIGINT PRIMARY KEY,
    job_card_id              BIGINT NOT NULL REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    material_sku_name        TEXT   NOT NULL,
    bom_id                   INT    REFERENCES bom_header(bom_id),
    bom_version              INT,
    bom_prescribed_qty       NUMERIC(15,3) NOT NULL CHECK (bom_prescribed_qty >= 0),
    actual_consumed_qty      NUMERIC(15,3) NOT NULL CHECK (actual_consumed_qty >= 0),
    uom                      TEXT NOT NULL CHECK (uom IN (
        'KGS','GMS','LTRS','NOS','PCS','ROLL','SETS','BUNDLE'
    )),
    variance_qty             NUMERIC(15,3) NOT NULL,
    variance_pct             NUMERIC(8,4),
    unit_cost_at_consumption NUMERIC(15,4),
    variance_cost_impact     NUMERIC(15,2),
    cost_basis               TEXT,
    recorded_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_card_id, material_sku_name)
);

CREATE INDEX IF NOT EXISTS idx_jcvar_jc          ON job_card_consumption_variance_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcvar_material    ON job_card_consumption_variance_v2(material_sku_name);
CREATE INDEX IF NOT EXISTS idx_jcvar_recorded_at ON job_card_consumption_variance_v2(recorded_at);

-- Trigger touches last_updated_at on every UPDATE.
CREATE OR REPLACE FUNCTION fn_touch_jcvar_last_updated()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jcvar_updated_at ON job_card_consumption_variance_v2;
CREATE TRIGGER trg_jcvar_updated_at
    BEFORE UPDATE ON job_card_consumption_variance_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_jcvar_last_updated();

-- 8. R12 - qc_notification_log_v2 (notify-QC dispatch audit)
CREATE TABLE IF NOT EXISTS qc_notification_log_v2 (
    notification_id   BIGINT PRIMARY KEY,
    job_card_id       BIGINT NOT NULL REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    recipient_user_id INT REFERENCES auth_user(user_id),
    dispatched_by     INT REFERENCES auth_user(user_id),
    channel           TEXT NOT NULL DEFAULT 'hook',
    note              TEXT,
    delivery_status   TEXT NOT NULL DEFAULT 'pending' CHECK (delivery_status IN (
        'pending', 'delivered', 'failed'
    )),
    delivery_meta     JSONB,
    dispatched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qcnotif_jc        ON qc_notification_log_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_qcnotif_recipient ON qc_notification_log_v2(recipient_user_id);
CREATE INDEX IF NOT EXISTS idx_qcnotif_status    ON qc_notification_log_v2(delivery_status);

-- 9. B0 - role rename stores_manager to inventory_manager (idempotent)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'stores_manager')
       AND NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'inventory_manager')
    THEN
        UPDATE auth_role
        SET    role_name   = 'inventory_manager',
               description = 'Inventory manager - stock, day-end, indents, GRN'
        WHERE  role_name   = 'stores_manager';
    END IF;
END $$;

-- 10. R8 - new role production_manager
INSERT INTO auth_role (role_name, description, is_admin) VALUES
    ('production_manager', 'Production manager - plan delete, stop process, unbalanced close approvals', FALSE)
ON CONFLICT (role_name) DO NOTHING;

-- 11. R8 / R12 - new permission rows
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description) VALUES
    ('production', 'amendments', NULL,     'view',     'View amendment requests'),
    ('production', 'amendments', NULL,     'create',   'File amendment request (maker)'),
    ('production', 'amendments', NULL,     'approve',  'Approve amendment request (checker)'),
    ('production', 'amendments', NULL,     'reject',   'Reject amendment request (checker)'),
    ('production', 'amendments', NULL,     'withdraw', 'Withdraw own amendment request (maker)'),
    ('production', 'qc',         'verify', 'create',   'Verify QC on JC (scoped qc_inspector only)'),
    ('production', 'qc',         'notify', 'create',   'Trigger QC notification on completed JC')
ON CONFLICT (module, sub_module, sub_sub_module, action) DO NOTHING;

COMMIT;
