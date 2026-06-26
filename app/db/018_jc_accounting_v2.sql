-- =========================================================================
-- Migration 018: Job Card v2 — Material Accounting (multi-stage aware)
--
-- v1 accounting assumed every JC produces FG. In a multi-stage flow only
-- the LAST stage produces FG; intermediate stages produce SFG/WIP that
-- carries to the next JC. The reconciliation has to balance against the
-- right UOM at each stage:
--
--   Stage 1   → input  = RM (kg/gms/ltrs/nos),  output = SFG (kg/pcs/nos)
--   Stage N   → input  = SFG from stage N-1,    output = SFG/WIP
--   Stage M   → input  = SFG from stage M-1,    output = FG (kg AND units)
--
-- Three tables:
--   job_card_material_consumption_v2  — per-input-line actual
--   job_card_byproducts_v2            — 10 fixed categories
--   job_card_accounting_v2            — summary + balance + loss breakdown
--
-- UOM CHECKs enforce the per-kind allow-list from app/.../services/uom.py
-- so a "process loss in BUNDLES" can't slip through.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- -----------------------------------------------------------------------
-- 1. job_card_material_consumption_v2 — actual qty consumed per input line
--
-- input_kind:
--   RM           — material from stores (stage 1 only)
--   SFG / WIP    — material carried from the previous stage's JC
--
-- For SFG/WIP inputs there's typically ONE row per JC: the qty carried
-- from prev_job_card_id. For RM there's one row per indent line.
--
-- variance = actual_consumed_qty - issued_qty. Auto-computed on save by
-- the service (kept as a column for cheap reads, not a generated column
-- because the inputs can be NULL during data entry).
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_material_consumption_v2 (
    -- App-supplied 8-digit time-based BIGINT (same pattern as
    -- so_fulfillment_id / plan_id / job_card_id). Caller uses
    -- new_short_time_id() + insert_with_pk_retry for the rare collision.
    consumption_id      BIGINT PRIMARY KEY,
    job_card_id         BIGINT NOT NULL
                        REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    -- For RM rows this is the RM SKU; for SFG rows it's a label like
    -- "SFG from JC #87234561 (Sorting)" so the operator sees the chain.
    material_sku_name   TEXT NOT NULL,
    input_kind          TEXT NOT NULL CHECK (input_kind IN ('RM','SFG','WIP','PM')),
    uom                 TEXT NOT NULL CHECK (uom IN (
                            -- Union of every per-kind allowed list:
                            'KGS','GMS','LTRS','NOS','PCS','ROLL','SETS','BUNDLE')),
    issued_qty          NUMERIC(15,3) NOT NULL CHECK (issued_qty >= 0),
    actual_consumed_qty NUMERIC(15,3) NOT NULL DEFAULT 0
                        CHECK (actual_consumed_qty >= 0),
    return_qty          NUMERIC(15,3) NOT NULL DEFAULT 0
                        CHECK (return_qty >= 0),
    variance            NUMERIC(15,3),
    -- Link back to the source row when this consumption corresponds to a
    -- specific RM indent or a specific prev-stage dispatch (audit trail).
    source_rm_indent_id BIGINT REFERENCES job_card_rm_indent_v2(rm_indent_id) ON DELETE SET NULL,
    source_dispatch_id  BIGINT REFERENCES job_card_partial_dispatch_v2(dispatch_id) ON DELETE SET NULL,
    remarks             TEXT,
    recorded_by         TEXT,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jcmc_v2_jc    ON job_card_material_consumption_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcmc_v2_kind  ON job_card_material_consumption_v2(input_kind);
-- One consumption row per (JC, material_sku) — re-saving updates the row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jcmc_v2_jc_material
    ON job_card_material_consumption_v2(job_card_id, material_sku_name);

COMMENT ON TABLE  job_card_material_consumption_v2 IS
    'Actual qty consumed per input line on a v2 JC. RM rows for stage 1; '
    'SFG/WIP rows for downstream stages (one row referencing the carried '
    'qty from the prev-stage dispatch).';

-- -----------------------------------------------------------------------
-- 2. job_card_byproducts_v2 — 10 fixed categories × per-JC
--
-- Categories are constrained so reports across plants stay comparable.
-- 'other' is the escape valve when none of the fixed categories fit.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_byproducts_v2 (
    byproduct_id        BIGINT PRIMARY KEY,  -- app-supplied 8-digit BIGINT
    job_card_id         BIGINT NOT NULL
                        REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    category            TEXT NOT NULL CHECK (category IN (
                            'tukda',          -- broken
                            'damaged',
                            'black_stained',
                            'without_shell',  -- kernels w/o shell
                            'empty_shells',
                            'dust',
                            'balance_material', -- leftover raw material
                            'rejection',
                            'control_sample',   -- QC retain
                            'other')),
    quantity            NUMERIC(15,3) NOT NULL CHECK (quantity >= 0),
    uom                 TEXT NOT NULL CHECK (uom IN (
                            'KGS','GMS','LTRS','NOS','PCS','ROLL','SETS','BUNDLE')),
    remarks             TEXT,
    recorded_by         TEXT,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One row per (JC, category). Re-saving updates the row.
    UNIQUE (job_card_id, category)
);

CREATE INDEX IF NOT EXISTS idx_jcbp_v2_jc ON job_card_byproducts_v2(job_card_id);

-- -----------------------------------------------------------------------
-- 3. job_card_accounting_v2 — summary balance row
--
-- One row per JC. Updated on each "Save & Validate" click; the saved
-- snapshot is what the close-JC pre-flight reads to gate the transition.
--
-- input_uom / output_uom: a stage may consume KGS and dispatch in PCS
-- (e.g. weighing line). Both are explicit so the math is auditable.
--
-- process_loss_breakdown: JSONB so the 6 sub-categories
-- (moisture/roasting/floor/dust/machine/sticky) don't need their own
-- columns AND callers can add new categories without a migration.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_accounting_v2 (
    accounting_id           BIGINT PRIMARY KEY,  -- app-supplied 8-digit BIGINT
    job_card_id             BIGINT NOT NULL UNIQUE
                            REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    -- Input side
    total_input_qty         NUMERIC(15,3) NOT NULL CHECK (total_input_qty >= 0),
    input_uom               TEXT NOT NULL CHECK (input_uom IN (
                                'KGS','GMS','LTRS','NOS','PCS','ROLL','SETS','BUNDLE')),
    -- Output side. For intermediate stages this is the SFG qty in the
    -- stage's UOM. For the FINAL stage we additionally capture the FG
    -- units count (since FG ships in pcs/units, not just kg).
    output_qty              NUMERIC(15,3) NOT NULL DEFAULT 0
                            CHECK (output_qty >= 0),
    output_uom              TEXT CHECK (output_uom IN (
                                'KGS','GMS','LTRS','NOS','PCS','ROLL','SETS','BUNDLE')),
    output_qty_units        NUMERIC(15,3),       -- only on final FG stage
    output_kind             TEXT NOT NULL CHECK (output_kind IN ('SFG','WIP','FG')),
    -- Carry-forward (mirrors job_card_v2 cols for cheap reads)
    carried_in_qty          NUMERIC(15,3) NOT NULL DEFAULT 0
                            CHECK (carried_in_qty >= 0),
    dispatched_out_qty      NUMERIC(15,3) NOT NULL DEFAULT 0
                            CHECK (dispatched_out_qty >= 0),
    -- Loss + leftover roll-ups
    process_loss_qty        NUMERIC(15,3) NOT NULL DEFAULT 0,
    process_loss_breakdown  JSONB NOT NULL DEFAULT '{}'::jsonb,
    extra_give_away_qty     NUMERIC(15,3) NOT NULL DEFAULT 0,
    balance_material_qty    NUMERIC(15,3) NOT NULL DEFAULT 0,
    offgrade_total_qty      NUMERIC(15,3) NOT NULL DEFAULT 0,
    rejection_qty           NUMERIC(15,3) NOT NULL DEFAULT 0,
    wastage_qty             NUMERIC(15,3) NOT NULL DEFAULT 0,
    control_sample_qty      NUMERIC(15,3) NOT NULL DEFAULT 0,
    -- Computed on save
    total_accounted_qty     NUMERIC(15,3) NOT NULL DEFAULT 0,
    balance_difference_qty  NUMERIC(15,3) NOT NULL DEFAULT 0,
    is_balanced             BOOLEAN NOT NULL DEFAULT FALSE,
    -- Loss %
    process_loss_pct        NUMERIC(6,3),
    other_loss_pct          NUMERIC(6,3),
    total_loss_pct          NUMERIC(6,3),
    -- Audit
    saved_by                TEXT,
    saved_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jca_v2_jc          ON job_card_accounting_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jca_v2_unbalanced  ON job_card_accounting_v2(job_card_id)
    WHERE is_balanced = FALSE;

-- updated_at touch (reuses the shared fn_touch_updated_at).
DROP TRIGGER IF EXISTS trg_jca_v2_updated_at ON job_card_accounting_v2;
CREATE TRIGGER trg_jca_v2_updated_at
    BEFORE UPDATE ON job_card_accounting_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
SELECT table_name
FROM   information_schema.tables
WHERE  table_name IN ('job_card_material_consumption_v2',
                       'job_card_byproducts_v2',
                       'job_card_accounting_v2')
ORDER  BY table_name;
