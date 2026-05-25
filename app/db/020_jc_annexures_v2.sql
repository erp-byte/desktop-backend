-- =========================================================================
-- Migration 020: v2 Quality Annexure tables
--
-- Five tables, one per annexure type. Each mirrors the v1 schema with:
--   • app-supplied 8-digit BIGINT PK (no BIGSERIAL default)
--   • FK to job_card_v2 instead of job_card
--   • soft-delete via deleted_at/deleted_by/deletion_reason
--   • audit columns (updated_at/updated_by) populated by service code
--
-- Tables:
--   job_card_metal_detection_v2      Annexure A — Fe/NFe/SS scans
--   job_card_weight_check_v2          Annexure B — per-sample weights
--   job_card_environment_v2           Annexure C — brine/temp/humidity/etc.
--   job_card_loss_reconciliation_v2   Annexure D — budgeted vs actual loss
--   job_card_remarks_v2               Annexure E — observations / deviations
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- -----------------------------------------------------------------------
-- Annexure A — Metal Detection
-- One row per (JC, check_type) wouldn't be right because the floor may
-- run multiple checks per shift; we keep multiple rows per JC and stamp
-- check_type so filters can split pre- vs post-packaging.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_metal_detection_v2 (
    detection_id    BIGINT PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    check_type      TEXT NOT NULL CHECK (check_type IN ('pre_packaging', 'post_packaging')),
    fe_pass         BOOLEAN,
    nfe_pass        BOOLEAN,
    ss_pass         BOOLEAN,
    failed_units    INT NOT NULL DEFAULT 0 CHECK (failed_units >= 0),
    remarks         TEXT,
    -- Audit + soft-delete
    recorded_by     TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    updated_by      TEXT,
    deleted_at      TIMESTAMPTZ,
    deleted_by      TEXT,
    deletion_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_jcmd_v2_jc ON job_card_metal_detection_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcmd_v2_active ON job_card_metal_detection_v2(job_card_id)
    WHERE deleted_at IS NULL;

-- -----------------------------------------------------------------------
-- Annexure B — Weight Checks
-- One row per sample. sample_number is operator-supplied so they can
-- record "Sample 5" even after deleting an earlier row.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_weight_check_v2 (
    check_id        BIGINT PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    sample_number   INT NOT NULL,
    net_weight      NUMERIC(15,3),
    gross_weight    NUMERIC(15,3),
    leak_test_pass  BOOLEAN,
    remarks         TEXT,
    -- Audit + soft-delete
    recorded_by     TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    updated_by      TEXT,
    deleted_at      TIMESTAMPTZ,
    deleted_by      TEXT,
    deletion_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_jcwc_v2_jc ON job_card_weight_check_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcwc_v2_active ON job_card_weight_check_v2(job_card_id)
    WHERE deleted_at IS NULL;

-- -----------------------------------------------------------------------
-- Annexure C — Environment
-- parameter_name is free-form to support arbitrary process probes —
-- brine_salinity, temp_c, humidity_pct, fan_pct, rpm, gas_ppm, magnet_kg.
-- Value stored as TEXT because the unit varies per parameter.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_environment_v2 (
    env_id          BIGINT PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    parameter_name  TEXT NOT NULL,
    value           TEXT,
    unit            TEXT,                          -- e.g. "°C", "%", "ppm", "kg"
    remarks         TEXT,
    -- Audit + soft-delete
    recorded_by     TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    updated_by      TEXT,
    deleted_at      TIMESTAMPTZ,
    deleted_by      TEXT,
    deletion_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_jcenv_v2_jc ON job_card_environment_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcenv_v2_active ON job_card_environment_v2(job_card_id)
    WHERE deleted_at IS NULL;

-- -----------------------------------------------------------------------
-- Annexure D — Loss Reconciliation (per-event evidence)
-- v2 keeps the JC-level loss roll-ups in job_card_accounting_v2.
-- This table is for per-event records (e.g. "sorting rejection at
-- 14:32, 2.3 kg, photographed") used as QC evidence during audits.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_loss_reconciliation_v2 (
    recon_id          BIGINT PRIMARY KEY,
    job_card_id       BIGINT NOT NULL
                      REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    loss_category     TEXT NOT NULL CHECK (loss_category IN (
                          'sorting_rejection', 'roasting_loss',
                          'packaging_rejection', 'metal_detector',
                          'spillage', 'qc_sample', 'other')),
    budgeted_loss_pct NUMERIC(5,3),
    budgeted_loss_qty NUMERIC(15,3),
    actual_loss_qty   NUMERIC(15,3),
    variance_qty      NUMERIC(15,3),               -- actual - budgeted
    uom               TEXT CHECK (uom IN ('KGS','GMS','LTRS','NOS','PCS','ROLL','SETS','BUNDLE')),
    remarks           TEXT,
    -- Audit + soft-delete
    recorded_by       TEXT,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ,
    updated_by        TEXT,
    deleted_at        TIMESTAMPTZ,
    deleted_by        TEXT,
    deletion_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jcloss_v2_jc ON job_card_loss_reconciliation_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcloss_v2_active ON job_card_loss_reconciliation_v2(job_card_id)
    WHERE deleted_at IS NULL;

-- -----------------------------------------------------------------------
-- Annexure E — Remarks
-- Three buckets: routine observation, deviation (process anomaly),
-- corrective_action (what was done about it).
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_remarks_v2 (
    remark_id       BIGINT PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    remark_type     TEXT NOT NULL CHECK (remark_type IN (
                        'observation', 'deviation', 'corrective_action')),
    content         TEXT NOT NULL,
    -- Audit + soft-delete
    recorded_by     TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    updated_by      TEXT,
    deleted_at      TIMESTAMPTZ,
    deleted_by      TEXT,
    deletion_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_jcrem_v2_jc ON job_card_remarks_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcrem_v2_active ON job_card_remarks_v2(job_card_id)
    WHERE deleted_at IS NULL;

-- Updated-at touch trigger (reuse the shared fn_touch_updated_at).
DROP TRIGGER IF EXISTS trg_jcmd_v2_updated_at  ON job_card_metal_detection_v2;
DROP TRIGGER IF EXISTS trg_jcwc_v2_updated_at  ON job_card_weight_check_v2;
DROP TRIGGER IF EXISTS trg_jcenv_v2_updated_at ON job_card_environment_v2;
DROP TRIGGER IF EXISTS trg_jcloss_v2_updated_at ON job_card_loss_reconciliation_v2;
DROP TRIGGER IF EXISTS trg_jcrem_v2_updated_at ON job_card_remarks_v2;

CREATE TRIGGER trg_jcmd_v2_updated_at  BEFORE UPDATE ON job_card_metal_detection_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_jcwc_v2_updated_at  BEFORE UPDATE ON job_card_weight_check_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_jcenv_v2_updated_at BEFORE UPDATE ON job_card_environment_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_jcloss_v2_updated_at BEFORE UPDATE ON job_card_loss_reconciliation_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
CREATE TRIGGER trg_jcrem_v2_updated_at BEFORE UPDATE ON job_card_remarks_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
SELECT table_name
FROM   information_schema.tables
WHERE  table_name LIKE 'job_card_%_v2'
  AND  table_name IN ('job_card_metal_detection_v2', 'job_card_weight_check_v2',
                       'job_card_environment_v2', 'job_card_loss_reconciliation_v2',
                       'job_card_remarks_v2')
ORDER  BY table_name;
