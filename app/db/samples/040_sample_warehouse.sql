-- =========================================================================
-- Migration 040: Sample module — rename entity -> warehouse + transport fields
--
-- The sample module's location axis is a physical WAREHOUSE, not a legal
-- entity. Rename sample_requisitions.entity / gate_passes.entity to `warehouse`
-- and re-point the CHECK to the warehouse code list. Add two optional transport
-- fields (transporter_name, vehicle_number) on the requisition.
--
-- The shared infra stays entity-scoped (cfpl/cdpl): material_document,
-- store_alert and production_order keep their own `entity` column, and the
-- sample services pass the legal entity 'cfpl' to them (warehouse is a separate
-- axis). If a warehouse must record under cdpl, map it in the service layer.
--
-- ASSUMES no pre-existing sample rows (module not yet in production); existing
-- 'cfpl'/'cdpl' values would violate the new warehouse CHECK — migrate them
-- first if the tables already hold data.
--
-- Idempotent. Safe to re-run.
-- =========================================================================
BEGIN;

-- ── sample_requisitions ─────────────────────────────────────────────────
ALTER TABLE sample_requisitions DROP CONSTRAINT IF EXISTS sample_requisitions_entity_check;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'sample_requisitions' AND column_name = 'entity') THEN
        ALTER TABLE sample_requisitions RENAME COLUMN entity TO warehouse;
    END IF;
END $$;
ALTER TABLE sample_requisitions DROP CONSTRAINT IF EXISTS sample_requisitions_warehouse_check;
ALTER TABLE sample_requisitions ADD CONSTRAINT sample_requisitions_warehouse_check
    CHECK (warehouse IN ('W202','A185','A68','F53','A101','D-39','D-514','Rishi','Supreme'));
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS transporter_name TEXT,   -- optional / nullable
    ADD COLUMN IF NOT EXISTS vehicle_number   TEXT;   -- optional / nullable

-- ── gate_passes ─────────────────────────────────────────────────────────
ALTER TABLE gate_passes DROP CONSTRAINT IF EXISTS gate_passes_entity_check;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'gate_passes' AND column_name = 'entity') THEN
        ALTER TABLE gate_passes RENAME COLUMN entity TO warehouse;
    END IF;
END $$;
ALTER TABLE gate_passes DROP CONSTRAINT IF EXISTS gate_passes_warehouse_check;
ALTER TABLE gate_passes ADD CONSTRAINT gate_passes_warehouse_check
    CHECK (warehouse IN ('W202','A185','A68','F53','A101','D-39','D-514','Rishi','Supreme'));

-- ── indexes (rename for clarity; no-op if already renamed) ───────────────
ALTER INDEX IF EXISTS idx_sample_req_entity  RENAME TO idx_sample_req_warehouse;
ALTER INDEX IF EXISTS idx_gate_passes_entity RENAME TO idx_gate_passes_warehouse;

COMMIT;
