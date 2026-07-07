-- =========================================================================
-- Migration 027: v2 surfaces for balance materials + QC
--
-- The /job-cards-v2/{id}/outputs endpoint was accepting `balance_materials`
-- and `qc` from the legacy v1 client but silently discarding them — there
-- was no v2 detail table for either. As a result the JC-detail GET could
-- never return them and the Android Output / Quality forms re-loaded blank
-- after save.
--
-- This migration creates the two missing v2 tables, mirroring v1's
-- `job_card_balance_material` (per-BOM-line balance rows) and a stripped
-- single-row QC summary that captures what the Android QualityFragment
-- needs. Both follow the v2 conventions established in migration 017:
--   * BIGINT app-supplied PK (8-digit short-time IDs)
--   * job_card_id FK with ON DELETE CASCADE
--   * recorded_by + recorded_at audit columns
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- -----------------------------------------------------------------------
-- 1. job_card_balance_material_v2 — per-BOM-line balance / leftover rows
--
-- Mirrors v1's job_card_balance_material. One row per (job_card_id,
-- bom_line_id, balance_type) — the operator can record both a "returned"
-- and a "wastage" entry for the same article, hence the composite
-- uniqueness instead of (job_card_id, bom_line_id) alone.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_balance_material_v2 (
    balance_id      BIGINT PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    bom_line_id     INT REFERENCES bom_line(bom_line_id),
    material_id     INT,
    material_name   TEXT NOT NULL,
    balance_type    TEXT NOT NULL CHECK (balance_type IN (
                        'extra_given', 'returned', 'wastage', 'control_sample')),
    qty_kg          NUMERIC(15,3) NOT NULL DEFAULT 0 CHECK (qty_kg >= 0),
    remarks         TEXT,
    recorded_by     TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_card_id, bom_line_id, balance_type)
);

CREATE INDEX IF NOT EXISTS idx_jcbm_v2_jc       ON job_card_balance_material_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcbm_v2_bom_line ON job_card_balance_material_v2(bom_line_id);

-- -----------------------------------------------------------------------
-- 2. job_card_qc_v2 — single-row QC summary per JC
--
-- A flat, append-no-history table — re-saves update the same row, keyed
-- on job_card_id. This matches the v1 surface QualityFragment consumes
-- (result / findings / corrective_action / inspector_user / inspection_date)
-- and is intentionally minimal — the rich qc_inspection table from v1
-- supports multiple checkpoint types; v2 keeps a single roll-up that the
-- mobile UI can round-trip.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_qc_v2 (
    qc_id             BIGINT PRIMARY KEY,
    job_card_id       BIGINT NOT NULL UNIQUE
                      REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    result            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (result IN ('pending','pass','fail','conditional_pass')),
    findings          TEXT,
    corrective_action TEXT,
    inspector_user    TEXT,
    inspection_date   TIMESTAMPTZ,
    recorded_by       TEXT,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jcqc_v2_jc     ON job_card_qc_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcqc_v2_result ON job_card_qc_v2(result);

-- Touch updated_at on re-save (reuses the shared fn from migration 018).
DROP TRIGGER IF EXISTS trg_jcqc_v2_updated_at ON job_card_qc_v2;
CREATE TRIGGER trg_jcqc_v2_updated_at
    BEFORE UPDATE ON job_card_qc_v2
    FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
SELECT table_name
FROM   information_schema.tables
WHERE  table_name IN ('job_card_balance_material_v2', 'job_card_qc_v2')
ORDER  BY table_name;
