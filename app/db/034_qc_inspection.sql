-- =========================================================================
-- 034_qc_inspection.sql
--
-- QC Inward Inspection — Phase 1: tables, indexes, and seed data.
--
-- IMPORTANT: this feature uses DEDICATED `qc_inward_*` tables. It deliberately
-- does NOT reuse the existing `qc_inspection` table from ims_new_schema.sql —
-- that one is the in-process / job-card QC checkpoint table (job_card_id NOT
-- NULL, checkpoint_type NOT NULL CHECK, inspection_id TEXT NOT NULL UNIQUE,
-- result NOT NULL …) and an inward-inspection row cannot satisfy those
-- constraints. Keeping the two paths in separate tables avoids the conflict.
--
-- Public API identifier: `inspection_id` = the PK of qc_inward_inspection.
--
-- PK CONVENTION: the transactional tables (qc_intimation, qc_inward_inspection,
-- qc_inward_reading, qc_inward_inspection_audit) use APP-SUPPLIED 8-digit
-- time-based BIGINT PKs (app.core.helpers.new_short_time_id + the
-- insert_with_pk_retry savepoint pattern), matching the v2 surface aligned by
-- migration 019. The columns are plain BIGINT with NO sequence default — the
-- application is responsible for supplying the value on insert. The
-- spec-master tables (qc_parameter) keep a SERIAL PK: they are seeded only via
-- SQL and never inserted from application code.
--
-- Fully idempotent — safe to re-run on any DB state. PostgreSQL ≥ 13.
-- =========================================================================


-- =========================================================================
-- 1. qc_intimation
--    Dock-arrival intimations that feed the inward inspection picker.
--    Population is external (e.g. Material In Send); seeded with test rows.
-- =========================================================================

CREATE TABLE IF NOT EXISTS qc_intimation (
    qc_intimation_id    BIGINT          PRIMARY KEY,   -- app-supplied 8-digit time id
    po_number           TEXT,
    transaction_no      TEXT,
    sku_id              INT,
    sku_name            TEXT,
    sku_name_raw        TEXT,
    supplier_id         INT,
    supplier_name       TEXT,
    lot_number          TEXT,
    vehicle_no          TEXT,
    warehouse           TEXT,
    entity              TEXT,
    status              TEXT            NOT NULL DEFAULT 'pending',
                                        -- pending | locked | consumed
    coa_received        BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qc_intimation_status
    ON qc_intimation(status);


-- =========================================================================
-- 2. qc_parameter  +  qc_sku_spec
--    Minimal spec master: named parameters (Acid Value, Moisture, …) and
--    per-SKU limit bands. Readings are hydrated against these at insert time.
-- =========================================================================

CREATE TABLE IF NOT EXISTS qc_parameter (
    parameter_id    SERIAL  PRIMARY KEY,
    name            TEXT    NOT NULL,
    unit            TEXT,
    UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS qc_sku_spec (
    sku_id          INT     NOT NULL,
    parameter_id    INT     NOT NULL REFERENCES qc_parameter(parameter_id),
    spec_min        NUMERIC,
    spec_max        NUMERIC,
    spec_target     NUMERIC,
    PRIMARY KEY (sku_id, parameter_id)
);


-- =========================================================================
-- 3. qc_inward_inspection  (dedicated inward-inspection table)
--    status: in_progress | readings_submitted | verdict_passed
--            | verdict_failed | cancelled
--    verdict: passed | failed
-- =========================================================================

CREATE TABLE IF NOT EXISTS qc_inward_inspection (
    inspection_id               BIGINT          PRIMARY KEY,   -- app-supplied 8-digit time id
    inspection_ref              TEXT,           -- optional human ref e.g. QCI-20260605-0001
    qc_intimation_id            BIGINT          REFERENCES qc_intimation(qc_intimation_id),
    status                      TEXT            NOT NULL DEFAULT 'in_progress',
    verdict                     TEXT,
    sample_size                 INT,
    inspection_method           TEXT,           -- visual | lab_test | combined
    inspector_user_id           INT,
    started_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    started_by                  INT,
    started_by_name             TEXT,
    verdict_at                  TIMESTAMPTZ,
    accepted_qty                NUMERIC,
    rejected_qty                NUMERIC,
    ncr_no                      TEXT,
    cancelled_at                TIMESTAMPTZ,
    cancelled_by                INT,
    cancelled_by_name           TEXT,
    cancel_reason               TEXT,
    reopened_at                 TIMESTAMPTZ,
    reopen_reason               TEXT,
    verdict_overridden_by       INT,
    verdict_overridden_by_name  TEXT,
    override_reason             TEXT,
    remarks                     TEXT,
    -- denormalized display fields (copied from intimation at start time)
    po_number                   TEXT,
    transaction_no              TEXT,
    sku_id                      INT,
    sku_name                    TEXT,
    sku_name_raw                TEXT,
    supplier_id                 INT,
    supplier_name               TEXT,
    lot_number                  TEXT,
    vehicle_no                  TEXT,
    warehouse                   TEXT,
    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qc_inward_status
    ON qc_inward_inspection(status);
CREATE INDEX IF NOT EXISTS idx_qc_inward_intimation
    ON qc_inward_inspection(qc_intimation_id);


-- =========================================================================
-- 4. qc_inward_reading
--    One row per parameter reading captured against an inward inspection.
-- =========================================================================

CREATE TABLE IF NOT EXISTS qc_inward_reading (
    reading_id              BIGINT      PRIMARY KEY,   -- app-supplied 8-digit time id
    inspection_id           BIGINT      NOT NULL
        REFERENCES qc_inward_inspection(inspection_id) ON DELETE CASCADE,
    parameter_id            INT,
    parameter_name          TEXT,
    parameter_unit          TEXT,
    observed_value_num      NUMERIC,
    observed_value_text     TEXT,
    spec_min                NUMERIC,
    spec_max                NUMERIC,
    spec_target             NUMERIC,
    is_within_spec          BOOLEAN,
    severity                TEXT,       -- minor | major | critical
    deviation_pct           NUMERIC,
    method                  TEXT,
    instrument              TEXT,
    notes                   TEXT,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qc_inward_reading_inspection
    ON qc_inward_reading(inspection_id);


-- =========================================================================
-- 5. qc_inward_inspection_audit
--    Append-only event log for every state transition / mutation.
-- =========================================================================

CREATE TABLE IF NOT EXISTS qc_inward_inspection_audit (
    audit_id        BIGINT          PRIMARY KEY,   -- app-supplied 8-digit time id
    inspection_id   BIGINT          NOT NULL
        REFERENCES qc_inward_inspection(inspection_id) ON DELETE CASCADE,
    event_type      TEXT            NOT NULL,
    from_state      TEXT,
    to_state        TEXT,
    actor_user_id   INT,
    payload_diff    JSONB,
    occurred_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qc_inward_audit_inspection
    ON qc_inward_inspection_audit(inspection_id);


-- =========================================================================
-- 6. SEED: qc_parameter
-- =========================================================================

INSERT INTO qc_parameter (name, unit)
VALUES
    ('Acid Value',      'mg KOH/g'),
    ('Moisture',        '%'),
    ('Peroxide Value',  'meq/kg')
ON CONFLICT (name) DO NOTHING;


-- =========================================================================
-- 7. SEED: qc_sku_spec  (sample sku_id = 1)
-- =========================================================================

DO $$
DECLARE
    v_acid_id       INT;
    v_moisture_id   INT;
    v_peroxide_id   INT;
BEGIN
    SELECT parameter_id INTO v_acid_id     FROM qc_parameter WHERE name = 'Acid Value';
    SELECT parameter_id INTO v_moisture_id FROM qc_parameter WHERE name = 'Moisture';
    SELECT parameter_id INTO v_peroxide_id FROM qc_parameter WHERE name = 'Peroxide Value';

    INSERT INTO qc_sku_spec (sku_id, parameter_id, spec_min, spec_max, spec_target)
    VALUES (1, v_acid_id, 0, 0.6, 0.3)
    ON CONFLICT (sku_id, parameter_id) DO NOTHING;

    INSERT INTO qc_sku_spec (sku_id, parameter_id, spec_min, spec_max, spec_target)
    VALUES (1, v_moisture_id, 0, 5, 2.5)
    ON CONFLICT (sku_id, parameter_id) DO NOTHING;

    INSERT INTO qc_sku_spec (sku_id, parameter_id, spec_min, spec_max, spec_target)
    VALUES (1, v_peroxide_id, 0, 10, 5)
    ON CONFLICT (sku_id, parameter_id) DO NOTHING;
END$$;


-- =========================================================================
-- 8. SEED: qc_intimation  (pending dock arrivals for the start-inspection
--    picker to test against). Guarded on transaction_no (no natural unique key).
-- =========================================================================

DO $$
BEGIN
    -- Seed rows use fixed, low BIGINT ids (not in the 8-digit time-id range,
    -- so they never collide with app-supplied ids) to keep the seed idempotent.
    IF NOT EXISTS (SELECT 1 FROM qc_intimation WHERE transaction_no = 'TR-2026-05-0831') THEN
        INSERT INTO qc_intimation
            (qc_intimation_id, po_number, transaction_no, sku_id, sku_name, sku_name_raw,
             supplier_id, supplier_name, lot_number, vehicle_no, warehouse, entity,
             status, coa_received)
        VALUES
            (1, 'PO-2026-00412', 'TR-2026-05-0831', 1,
             'Refined Sunflower Oil 15 kg', 'RSFN-15KG',
             101, 'Adani Wilmar Ltd', 'LOT-AW-260528-01', 'MH-05-BV-9234',
             'PUNE-WH-A', 'cfpl', 'pending', FALSE);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM qc_intimation WHERE transaction_no = 'TR-2026-05-0844') THEN
        INSERT INTO qc_intimation
            (qc_intimation_id, po_number, transaction_no, sku_id, sku_name, sku_name_raw,
             supplier_id, supplier_name, lot_number, vehicle_no, warehouse, entity,
             status, coa_received)
        VALUES
            (2, 'PO-2026-00418', 'TR-2026-05-0844', 2,
             'Palm Olein (RBD) 15 kg', 'PLMO-15KG',
             102, '3F Industries Ltd', 'LOT-3F-260530-04', 'MH-14-GJ-7721',
             'PUNE-WH-A', 'cfpl', 'pending', FALSE);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM qc_intimation WHERE transaction_no = 'TR-2026-06-0003') THEN
        INSERT INTO qc_intimation
            (qc_intimation_id, po_number, transaction_no, sku_id, sku_name, sku_name_raw,
             supplier_id, supplier_name, lot_number, vehicle_no, warehouse, entity,
             status, coa_received)
        VALUES
            (3, 'PO-2026-00425', 'TR-2026-06-0003', 3,
             'Groundnut Oil (Filtered) 15 kg', 'GNUT-15KG',
             103, 'Gokul Agro Resources Ltd', 'LOT-GR-260601-02', 'GJ-01-CD-4450',
             'PUNE-WH-B', 'cdpl', 'pending', TRUE);
    END IF;
END$$;
