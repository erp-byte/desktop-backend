-- =========================================================================
-- Migration 036: Generic gate-pass infrastructure + sample movement types
--
-- Per the approved checklist, gate passes are built as a GENERIC system
-- table (gate_passes) rather than a sample-only table, so a future
-- warehouse-transfer / dispatch gate pass can reuse it. The Sample Issuing
-- module is its first consumer. Sample-specific fields live in a 1:1
-- companion table (gate_pass_sample_details) created in migration 037,
-- AFTER sample_requisitions exists.
--
-- Also registers the two sample movement types in movement_type_ref:
--   265 — Goods Issue to Sample        (reversed by 266)
--   266 — Reversal of Sample Goods Issue
-- The reference_type value 'SAMPLE_REQ' is convention only (reference_type
-- is a free-text column on material_document — no DDL needed).
--
-- Depends on: sap_mm_align.sql (material_document, movement_type_ref),
--             auth_schema.sql (auth_user). No dependency on sample tables.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- 1. Generic gate-pass header --------------------------------------------
CREATE TABLE IF NOT EXISTS gate_passes (
    id                   SERIAL PRIMARY KEY,
    gate_pass_number     TEXT NOT NULL UNIQUE,                -- GP-YYYYMMDD-NNNN  e.g. GP-20260603-0042 (service _gen_id pattern, seq_gate_pass)
    gate_pass_type       TEXT NOT NULL DEFAULT 'SAMPLE'
                         CHECK (gate_pass_type IN ('SAMPLE','TRANSFER','OTHER')),
    -- Polymorphic source reference (SAMPLE -> source_ref_type='SAMPLE_REQ',
    -- source_ref_id = sample_requisitions.id). Kept generic so non-sample
    -- pass types can point at their own source rows.
    source_ref_type      TEXT,
    source_ref_id        INT,
    material_document_id  INT REFERENCES material_document(id),
    -- Route / recipient (generic: warehouse-to-warehouse OR customer)
    from_location        TEXT,
    to_location          TEXT,
    recipient_name       TEXT,
    recipient_contact    TEXT,
    vehicle_carrier      TEXT,
    driver_name          TEXT,
    -- Issuance + approval snapshots (generic two-step approval; for SAMPLE,
    -- approver1 = Business Head, approver2 = Inventory Manager).
    issued_by            INT NOT NULL REFERENCES auth_user(user_id),
    issued_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approver1_user_id    INT REFERENCES auth_user(user_id),
    approver1_at         TIMESTAMPTZ,
    approver2_user_id    INT REFERENCES auth_user(user_id),
    approver2_at         TIMESTAMPTZ,
    -- Print bookkeeping (DB row is the source of truth, never the PDF)
    print_count          INT NOT NULL DEFAULT 0,
    last_printed_at      TIMESTAMPTZ,
    -- Void (no auto-reversal of stock; manual 266 entry handles that)
    voided               BOOLEAN NOT NULL DEFAULT FALSE,
    voided_at            TIMESTAMPTZ,
    voided_by            INT REFERENCES auth_user(user_id),
    void_reason          TEXT,
    entity               TEXT NOT NULL CHECK (entity IN ('cfpl','cdpl')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gate_passes_type   ON gate_passes(gate_pass_type);
CREATE INDEX IF NOT EXISTS idx_gate_passes_source ON gate_passes(source_ref_type, source_ref_id);
CREATE INDEX IF NOT EXISTS idx_gate_passes_issued ON gate_passes(issued_at);
CREATE INDEX IF NOT EXISTS idx_gate_passes_voided ON gate_passes(voided);
CREATE INDEX IF NOT EXISTS idx_gate_passes_entity ON gate_passes(entity);

-- Service formats the number as GP-YYYYMMDD-NNNN (same _gen_id house pattern
-- as MATDOC/ISN/etc.: 8-digit date + nextval). The sequence guarantees a
-- collision-free suffix under concurrent issuance (no MAX(id)+1).
CREATE SEQUENCE IF NOT EXISTS seq_gate_pass START 1;

-- 2. Sample movement types in the SAP registry --------------------------
INSERT INTO movement_type_ref (movement_type, description, direction, affects_stock, reversal_type) VALUES
    ('265', 'Goods Issue to Sample',          'OUT',      TRUE, '266'),
    ('266', 'Reversal of Sample Goods Issue', 'REVERSAL', TRUE, NULL)
ON CONFLICT (movement_type) DO NOTHING;

COMMIT;
