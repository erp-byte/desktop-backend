-- =========================================================================
-- 009_planning_v2.sql
-- Production planning v2 schema for so_fulfillment, production_plan,
-- production_plan_line. New tables suffixed _v2 to coexist with v1 (which
-- is heavily referenced by downstream tables such as fulfillment_floor_stock,
-- fulfillment_bom_override, purchase_indent, production_order, etc.).
--
-- Highlights vs v1:
--   • BIGSERIAL/BIGINT keys (was SERIAL/INT)
--   • so_fulfillment_v2.pending_qty_kg is a GENERATED STORED column
--   • Tight CHECK constraints on entity, status, priority, warehouse, shift
--   • production_plan_v2 requires approved_by + approved_at when approved
--   • production_plan_line_v2 enforces FK ON DELETE CASCADE to plan and
--     ON DELETE RESTRICT to bom_header
--   • GIN index on linked_so_fulfillment_ids
--
-- Idempotent. Safe to re-run. PostgreSQL ≥ 12 (uses GENERATED ... STORED).
-- =========================================================================

-- ----------------------------------------------------------------------
-- Shared trigger function for updated_at maintenance (idempotent)
-- ----------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ----------------------------------------------------------------------
-- 1. so_fulfillment_v2   demand-side ledger
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS so_fulfillment_v2 (
    -- so_fulfillment_id is supplied by the application using a time-based
    -- short ID (last 8 digits of epoch-ms), not auto-incremented.
    so_fulfillment_id    BIGINT        PRIMARY KEY,
    so_line_id           BIGINT        NOT NULL,
    financial_year       VARCHAR(7)    NOT NULL,
    fg_sku_name          TEXT          NOT NULL,
    customer_name        TEXT          NOT NULL,
    entity               VARCHAR(10)   NOT NULL
                         CHECK (entity IN ('cfpl','cdpl')),
    original_qty_kg      NUMERIC(12,3) NOT NULL CHECK (original_qty_kg   >= 0),
    produced_qty_kg      NUMERIC(12,3) NOT NULL DEFAULT 0
                                                  CHECK (produced_qty_kg   >= 0),
    dispatched_qty_kg    NUMERIC(12,3) NOT NULL DEFAULT 0
                                                  CHECK (dispatched_qty_kg >= 0),
    pending_qty_kg       NUMERIC(12,3) GENERATED ALWAYS AS
                         (original_qty_kg - dispatched_qty_kg) STORED,
    -- Units are tracked separately from kg (SO carries both: pack count and weight).
    -- original_qty_units is nullable because so_line.quantity_units may be NULL upstream.
    original_qty_units   NUMERIC(12,3) CHECK (original_qty_units IS NULL
                                              OR original_qty_units >= 0),
    produced_qty_units   NUMERIC(12,3) NOT NULL DEFAULT 0
                                                  CHECK (produced_qty_units   >= 0),
    dispatched_qty_units NUMERIC(12,3) NOT NULL DEFAULT 0
                                                  CHECK (dispatched_qty_units >= 0),
    pending_qty_units    NUMERIC(12,3) GENERATED ALWAYS AS
                         (original_qty_units - dispatched_qty_units) STORED,
    priority             SMALLINT      NOT NULL DEFAULT 5
                                                  CHECK (priority BETWEEN 1 AND 10),
    deadline_date        DATE,
    order_status         VARCHAR(20)   NOT NULL DEFAULT 'open'
                         CHECK (order_status IN ('open','partial','fulfilled',
                                                 'cancelled')),
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (so_line_id, financial_year)
);

CREATE INDEX IF NOT EXISTS idx_sof_v2_entity_status
    ON so_fulfillment_v2(entity, order_status);
CREATE INDEX IF NOT EXISTS idx_sof_v2_fg_customer
    ON so_fulfillment_v2(fg_sku_name, customer_name);
CREATE INDEX IF NOT EXISTS idx_sof_v2_open_priority
    ON so_fulfillment_v2(priority, deadline_date)
    WHERE order_status IN ('open','partial');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_sof_v2_touch'
    ) THEN
        CREATE TRIGGER trg_sof_v2_touch
            BEFORE UPDATE ON so_fulfillment_v2
            FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();
    END IF;
END$$;


-- ----------------------------------------------------------------------
-- 1a. so_revision_log_v2   audit trail for qty/date revisions
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS so_revision_log_v2 (
    revision_id          BIGSERIAL PRIMARY KEY,
    so_fulfillment_id    BIGINT NOT NULL
                         REFERENCES so_fulfillment_v2(so_fulfillment_id)
                         ON DELETE CASCADE,
    revision_type        VARCHAR(20) NOT NULL
                         CHECK (revision_type IN ('qty_change','units_change','date_change')),
    old_value            TEXT,
    new_value            TEXT,
    reason               TEXT,
    revised_by           TEXT,
    revised_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_srl_v2_fulfillment
    ON so_revision_log_v2(so_fulfillment_id);


-- ----------------------------------------------------------------------
-- 2. production_plan_v2   plan header
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS production_plan_v2 (
    plan_id          BIGSERIAL PRIMARY KEY,
    entity           VARCHAR(10) NOT NULL CHECK (entity    IN ('cfpl','cdpl')),
    warehouse        VARCHAR(10) NOT NULL CHECK (warehouse IN ('W-202','A-185')),
    plan_type        VARCHAR(10) NOT NULL CHECK (plan_type IN ('daily','weekly')),
    plan_date        DATE        NOT NULL,
    date_from        DATE        NOT NULL,
    date_to          DATE        NOT NULL,
    status           VARCHAR(15) NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','approved','executed','cancelled')),
    revision_number  SMALLINT    NOT NULL DEFAULT 0 CHECK (revision_number >= 0),
    previous_plan_id BIGINT REFERENCES production_plan_v2(plan_id) ON DELETE SET NULL,
    approved_by      TEXT,
    approved_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (date_to >= date_from),
    CHECK (
        (status =  'approved' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
        OR status <> 'approved'
    )
);

CREATE INDEX IF NOT EXISTS idx_pp_v2_entity_wh_date
    ON production_plan_v2(entity, warehouse, plan_date);
CREATE INDEX IF NOT EXISTS idx_pp_v2_status
    ON production_plan_v2(status);
CREATE INDEX IF NOT EXISTS idx_pp_v2_revision_chain
    ON production_plan_v2(previous_plan_id)
    WHERE previous_plan_id IS NOT NULL;


-- ----------------------------------------------------------------------
-- 3. production_plan_line_v2   plan detail
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS production_plan_line_v2 (
    plan_line_id              BIGSERIAL PRIMARY KEY,
    plan_id                   BIGINT NOT NULL
                              REFERENCES production_plan_v2(plan_id) ON DELETE CASCADE,
    fg_sku_name               TEXT   NOT NULL,
    customer_name             TEXT,
    bom_id                    BIGINT NOT NULL
                              REFERENCES bom_header(bom_id) ON DELETE RESTRICT,
    planned_qty_kg            NUMERIC(12,3) NOT NULL CHECK (planned_qty_kg    > 0),
    planned_qty_units         NUMERIC(12,3) NOT NULL CHECK (planned_qty_units > 0),
    area                      VARCHAR(30),
    priority                  SMALLINT    NOT NULL DEFAULT 5
                                                   CHECK (priority BETWEEN 1 AND 10),
    shift                     VARCHAR(10) CHECK (shift IN ('A','B','C','general')),
    stage_sequence            TEXT[],
    estimated_hours           NUMERIC(8,2) CHECK (estimated_hours >= 0),
    linked_so_fulfillment_ids BIGINT[],
    status                    VARCHAR(15) NOT NULL DEFAULT 'planned'
                              CHECK (status IN ('planned','in_progress',
                                                'completed','cancelled')),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ppl_v2_plan
    ON production_plan_line_v2(plan_id);
CREATE INDEX IF NOT EXISTS idx_ppl_v2_bom
    ON production_plan_line_v2(bom_id);
CREATE INDEX IF NOT EXISTS idx_ppl_v2_fg_customer
    ON production_plan_line_v2(fg_sku_name, customer_name);
CREATE INDEX IF NOT EXISTS idx_ppl_v2_status
    ON production_plan_line_v2(status);
CREATE INDEX IF NOT EXISTS idx_ppl_v2_linked_so_gin
    ON production_plan_line_v2 USING GIN (linked_so_fulfillment_ids);


-- ----------------------------------------------------------------------
-- Documentation comments
-- ----------------------------------------------------------------------
COMMENT ON TABLE  so_fulfillment_v2 IS
    'v2 demand-side ledger fed from external SO module; carries running pending qty per SO line.';
COMMENT ON COLUMN so_fulfillment_v2.pending_qty_kg IS
    'Auto-computed: original_qty_kg - dispatched_qty_kg (generated stored column).';
COMMENT ON COLUMN so_fulfillment_v2.pending_qty_units IS
    'Auto-computed: original_qty_units - dispatched_qty_units (generated stored column). NULL if original_qty_units is unknown.';

COMMENT ON TABLE  so_revision_log_v2 IS
    'Audit trail for qty and deadline revisions on so_fulfillment_v2 rows.';

COMMENT ON TABLE  production_plan_v2 IS
    'v2 plan header; one row per warehouse + plan_date. Revisions chain via previous_plan_id.';
COMMENT ON COLUMN production_plan_v2.revision_number IS
    'Starts at 0; incremented when a revised plan is created.';

COMMENT ON TABLE  production_plan_line_v2 IS
    'v2 plan detail; one row per FG to produce. Links to BOM and to satisfying SO fulfillments.';
COMMENT ON COLUMN production_plan_line_v2.linked_so_fulfillment_ids IS
    'Array of so_fulfillment_v2.so_fulfillment_id values this plan line is allocated against (M-to-N).';
COMMENT ON COLUMN production_plan_line_v2.stage_sequence IS
    'Ordered list of process stage names from bom_process_route, snapshotted at plan creation.';
