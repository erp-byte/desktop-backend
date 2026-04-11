-- Production Planning Module tables (v1)

-- =========================================================================
-- MASTER DATA
-- =========================================================================

-- Machines (physical equipment on the factory floor)
CREATE TABLE IF NOT EXISTS machine (
    machine_id              SERIAL PRIMARY KEY,
    machine_name            TEXT NOT NULL,
    machine_type            TEXT,                          -- sorting_table, sealer, scale, metal_detector, etc.
    category                TEXT,                          -- processing, packaging, quality
    capable_stages          TEXT[],                        -- {'sorting','grading','weighing'}
    floor                   TEXT,                          -- production floor name
    factory                 TEXT,                          -- W-202, A-185
    status                  TEXT NOT NULL DEFAULT 'active', -- active, maintenance, retired
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_machine_status ON machine(status);
CREATE INDEX IF NOT EXISTS idx_machine_entity ON machine(entity);

-- Machine capacity per product-group per stage
CREATE TABLE IF NOT EXISTS machine_capacity (
    capacity_id             SERIAL PRIMARY KEY,
    machine_id              INT NOT NULL REFERENCES machine(machine_id),
    stage                   TEXT NOT NULL,                 -- sorting, weighing, sealing, metal_detection
    item_group              TEXT NOT NULL,                 -- cashew, almond, dates, seeds, raisin
    capacity_kg_per_hr      NUMERIC(15,3) NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (machine_id, stage, item_group)
);

CREATE INDEX IF NOT EXISTS idx_machine_capacity_machine ON machine_capacity(machine_id);

-- BOM header (one per FG-customer-pack combination)
CREATE TABLE IF NOT EXISTS bom_header (
    bom_id                  SERIAL PRIMARY KEY,
    fg_sku_name             TEXT NOT NULL,                 -- finished good name
    customer_name           TEXT,                          -- NULL = generic BOM
    pack_size_kg            NUMERIC(15,3),                 -- 0.25, 0.5, 1.0, etc.
    version                 INT NOT NULL DEFAULT 1,
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    effective_from          DATE,
    effective_to            DATE,                          -- NULL = no expiry
    item_group              TEXT,                          -- matched from all_sku
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bom_header_fg ON bom_header(fg_sku_name);
CREATE INDEX IF NOT EXISTS idx_bom_header_entity ON bom_header(entity);
CREATE INDEX IF NOT EXISTS idx_bom_header_active ON bom_header(is_active);

-- BOM line items (materials: RM + PM)
CREATE TABLE IF NOT EXISTS bom_line (
    bom_line_id             SERIAL PRIMARY KEY,
    bom_id                  INT NOT NULL REFERENCES bom_header(bom_id),
    line_number             INT NOT NULL,
    material_sku_name       TEXT NOT NULL,
    item_type               TEXT NOT NULL,                 -- rm, pm
    quantity_per_unit        NUMERIC(15,3) NOT NULL,       -- kg or pcs per 1 unit of FG
    uom                     TEXT,                          -- kg, pcs
    loss_pct                NUMERIC(5,3) DEFAULT 0,        -- expected loss %
    godown                  TEXT,                          -- RM Store, PM Store
    can_use_offgrade        BOOLEAN DEFAULT FALSE,
    offgrade_max_pct        NUMERIC(5,3) DEFAULT 0,        -- max substitution %
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bom_id, line_number)
);

CREATE INDEX IF NOT EXISTS idx_bom_line_bom ON bom_line(bom_id);

-- BOM process route (sequential manufacturing steps)
CREATE TABLE IF NOT EXISTS bom_process_route (
    route_id                SERIAL PRIMARY KEY,
    bom_id                  INT NOT NULL REFERENCES bom_header(bom_id),
    step_number             INT NOT NULL,
    process_name            TEXT NOT NULL,                 -- Sorting, Weighing, Sealing, Metal Detection
    stage                   TEXT NOT NULL,                 -- sorting, weighing, sealing, metal_detection
    std_time_min            NUMERIC(10,2),                 -- standard time in minutes
    loss_pct                NUMERIC(5,3) DEFAULT 0,
    qc_check                TEXT,                          -- visual+FM, net weight ±2g, seal integrity, Fe/Nfe/SS
    machine_type            TEXT,                          -- type of machine needed
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bom_id, step_number)
);

CREATE INDEX IF NOT EXISTS idx_bom_route_bom ON bom_process_route(bom_id);

-- =========================================================================
-- PLANNING
-- =========================================================================

-- SO fulfillment tracking (bridges SO module to production)
CREATE TABLE IF NOT EXISTS so_fulfillment (
    fulfillment_id          SERIAL PRIMARY KEY,
    so_line_id              INT NOT NULL,                  -- FK to so_line.so_line_id
    so_id                   INT,                           -- FK to so_header.so_id
    financial_year          TEXT NOT NULL,                  -- "2025-26", "2026-27"
    fg_sku_name             TEXT NOT NULL,
    customer_name           TEXT,
    original_qty_kg         NUMERIC(15,3) NOT NULL,
    revised_qty_kg          NUMERIC(15,3),
    pending_qty_kg          NUMERIC(15,3) NOT NULL,
    produced_qty_kg         NUMERIC(15,3) DEFAULT 0,
    dispatched_qty_kg       NUMERIC(15,3) DEFAULT 0,
    order_status            TEXT NOT NULL DEFAULT 'open',  -- open, partial, fulfilled, carryforward, cancelled
    delivery_deadline       DATE,
    priority                INT DEFAULT 5,                 -- 1=highest, 10=lowest
    carryforward_from_id    INT REFERENCES so_fulfillment(fulfillment_id),
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (so_line_id, financial_year)
);

CREATE INDEX IF NOT EXISTS idx_fulfillment_status ON so_fulfillment(order_status);
CREATE INDEX IF NOT EXISTS idx_fulfillment_fy ON so_fulfillment(financial_year);
CREATE INDEX IF NOT EXISTS idx_fulfillment_entity ON so_fulfillment(entity);

-- SO revision audit log
CREATE TABLE IF NOT EXISTS so_revision_log (
    revision_id             SERIAL PRIMARY KEY,
    fulfillment_id          INT NOT NULL REFERENCES so_fulfillment(fulfillment_id),
    revision_type           TEXT NOT NULL,                 -- qty_change, date_change, carryforward, cancel
    old_value               TEXT,
    new_value               TEXT,
    reason                  TEXT,
    revised_by              TEXT,
    revised_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_revision_fulfillment ON so_revision_log(fulfillment_id);

-- Production plan header
CREATE TABLE IF NOT EXISTS production_plan (
    plan_id                 SERIAL PRIMARY KEY,
    plan_name               TEXT,
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    plan_type               TEXT NOT NULL DEFAULT 'daily', -- daily, weekly, full
    plan_date               DATE NOT NULL,                 -- the day this plan was created
    date_from               DATE NOT NULL,                 -- plan covers from
    date_to                 DATE NOT NULL,                 -- plan covers to
    status                  TEXT NOT NULL DEFAULT 'draft', -- draft, approved, executed, cancelled
    ai_generated            BOOLEAN DEFAULT FALSE,
    ai_analysis_json        JSONB,                         -- full Claude response
    revision_number         INT DEFAULT 1,
    previous_plan_id        INT REFERENCES production_plan(plan_id),
    approved_by             TEXT,
    approved_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plan_status ON production_plan(status);
CREATE INDEX IF NOT EXISTS idx_plan_entity ON production_plan(entity);
CREATE INDEX IF NOT EXISTS idx_plan_date ON production_plan(plan_date);

-- Production plan line items
CREATE TABLE IF NOT EXISTS production_plan_line (
    plan_line_id            SERIAL PRIMARY KEY,
    plan_id                 INT NOT NULL REFERENCES production_plan(plan_id),
    fg_sku_name             TEXT NOT NULL,
    customer_name           TEXT,
    bom_id                  INT REFERENCES bom_header(bom_id),
    planned_qty_kg          NUMERIC(15,3) NOT NULL,
    planned_qty_units       INT,
    machine_id              INT REFERENCES machine(machine_id),
    priority                INT DEFAULT 5,
    shift                   TEXT,                          -- day, night
    stage_sequence          TEXT[],                        -- {'sorting','weighing','sealing','metal_detection'}
    estimated_hours         NUMERIC(10,2),
    linked_so_fulfillment_ids INT[],                      -- array of fulfillment_ids
    reasoning               TEXT,                          -- Claude's reasoning
    status                  TEXT NOT NULL DEFAULT 'planned', -- planned, in_progress, completed, cancelled
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plan_line_plan ON production_plan_line(plan_id);

-- =========================================================================
-- EXECUTION
-- =========================================================================

-- Production order (created from approved plan line)
CREATE TABLE IF NOT EXISTS production_order (
    prod_order_id           SERIAL PRIMARY KEY,
    prod_order_number       TEXT NOT NULL UNIQUE,          -- PO-2026-0042
    plan_line_id            INT REFERENCES production_plan_line(plan_line_id),
    bom_id                  INT REFERENCES bom_header(bom_id),
    fg_sku_name             TEXT NOT NULL,
    customer_name           TEXT,
    batch_number            TEXT NOT NULL UNIQUE,           -- B2026-042
    batch_size_kg           NUMERIC(15,3) NOT NULL,
    net_wt_per_unit         NUMERIC(15,3),                 -- pack size in kg
    best_before             DATE,
    total_stages            INT NOT NULL DEFAULT 1,
    status                  TEXT NOT NULL DEFAULT 'created', -- created, job_cards_issued, in_progress, completed, cancelled
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    factory                 TEXT,
    floor                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prod_order_status ON production_order(status);
CREATE INDEX IF NOT EXISTS idx_prod_order_entity ON production_order(entity);

-- Job card (one per process stage per production order)
CREATE TABLE IF NOT EXISTS job_card (
    job_card_id             SERIAL PRIMARY KEY,
    job_card_number         TEXT NOT NULL UNIQUE,           -- PO-2026-0042/1
    prod_order_id           INT NOT NULL REFERENCES production_order(prod_order_id),
    bom_id                  INT REFERENCES bom_header(bom_id),
    step_number             INT NOT NULL,                   -- 1, 2, 3...
    process_name            TEXT NOT NULL,                   -- Sorting, Weighing, etc.
    stage                   TEXT NOT NULL,                   -- sorting, weighing, etc.
    -- Product details (denormalized from production_order for quick access)
    fg_sku_name             TEXT NOT NULL,
    customer_name           TEXT,
    batch_number            TEXT NOT NULL,
    batch_size_kg           NUMERIC(15,3) NOT NULL,
    -- Machine assignment
    machine_id              INT REFERENCES machine(machine_id),
    -- Team
    assigned_to_team_leader TEXT,
    team_members            TEXT[],
    -- Locking
    is_locked               BOOLEAN NOT NULL DEFAULT TRUE,
    locked_reason           TEXT,                           -- awaiting_previous_stage, material_pending
    force_unlocked          BOOLEAN DEFAULT FALSE,
    force_unlock_by         TEXT,
    force_unlock_reason     TEXT,
    force_unlock_at         TIMESTAMPTZ,
    -- Lifecycle
    status                  TEXT NOT NULL DEFAULT 'locked', -- locked, unlocked, assigned, material_received, in_progress, completed, closed
    start_time              TIMESTAMPTZ,
    end_time                TIMESTAMPTZ,
    total_time_min          NUMERIC(10,2),
    -- Location
    factory                 TEXT,
    floor                   TEXT,
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    -- Chaining for multi-stage partial dispatch
    next_job_card_id        INT REFERENCES job_card(job_card_id),
    prev_job_card_id        INT REFERENCES job_card(job_card_id),
    carried_qty_kg          NUMERIC(15,3) NOT NULL DEFAULT 0,  -- received from prev stage
    dispatched_to_next_kg   NUMERIC(15,3) NOT NULL DEFAULT 0,  -- pushed to next stage
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_job_card_prod_order ON job_card(prod_order_id);
CREATE INDEX IF NOT EXISTS idx_job_card_status ON job_card(status);
CREATE INDEX IF NOT EXISTS idx_job_card_entity ON job_card(entity);
CREATE INDEX IF NOT EXISTS idx_job_card_team_leader ON job_card(assigned_to_team_leader);
CREATE INDEX IF NOT EXISTS idx_job_card_next ON job_card(next_job_card_id);
CREATE INDEX IF NOT EXISTS idx_job_card_prev ON job_card(prev_job_card_id);

-- Partial dispatch log (chunked handoffs between chained job cards)
CREATE TABLE IF NOT EXISTS job_card_partial_dispatch (
    dispatch_id             SERIAL PRIMARY KEY,
    from_job_card_id        INT NOT NULL REFERENCES job_card(job_card_id),
    to_job_card_id          INT NOT NULL REFERENCES job_card(job_card_id),
    qty_kg                  NUMERIC(15,3) NOT NULL CHECK (qty_kg > 0),
    dispatched_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_by           TEXT
);

CREATE INDEX IF NOT EXISTS idx_jcpd_from ON job_card_partial_dispatch(from_job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcpd_to   ON job_card_partial_dispatch(to_job_card_id);

-- Job card RM indent (raw material requirements)
CREATE TABLE IF NOT EXISTS job_card_rm_indent (
    rm_indent_id            SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    material_sku_name       TEXT NOT NULL,
    uom                     TEXT,                          -- kg
    reqd_qty                NUMERIC(15,3) NOT NULL,
    loss_pct                NUMERIC(5,3) DEFAULT 0,
    gross_qty               NUMERIC(15,3) NOT NULL,        -- reqd / (1 - loss%)
    issued_qty              NUMERIC(15,3) DEFAULT 0,
    batch_no                TEXT,                           -- lot number from po_box
    godown                  TEXT,
    scanned_box_ids         TEXT[],                        -- array of po_box.box_id
    variance                NUMERIC(15,3),                 -- issued - gross
    status                  TEXT NOT NULL DEFAULT 'pending', -- pending, partial, fulfilled
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rm_indent_jc ON job_card_rm_indent(job_card_id);

-- Job card PM indent (packaging material requirements)
CREATE TABLE IF NOT EXISTS job_card_pm_indent (
    pm_indent_id            SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    material_sku_name       TEXT NOT NULL,
    uom                     TEXT,                          -- pcs
    reqd_qty                NUMERIC(15,3) NOT NULL,
    loss_pct                NUMERIC(5,3) DEFAULT 0,
    gross_qty               NUMERIC(15,3) NOT NULL,
    issued_qty              NUMERIC(15,3) DEFAULT 0,
    batch_no                TEXT,
    godown                  TEXT,
    scanned_box_ids         TEXT[],
    variance                NUMERIC(15,3),
    status                  TEXT NOT NULL DEFAULT 'pending',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pm_indent_jc ON job_card_pm_indent(job_card_id);

-- Job card process steps (within a single job card)
CREATE TABLE IF NOT EXISTS job_card_process_step (
    step_id                 SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    step_number             INT NOT NULL,
    process_name            TEXT NOT NULL,
    machine_name            TEXT,
    std_time_min            NUMERIC(10,2),
    qc_check                TEXT,
    loss_pct                NUMERIC(5,3) DEFAULT 0,
    operator_name           TEXT,
    operator_sign_at        TIMESTAMPTZ,
    qc_sign_at              TIMESTAMPTZ,
    time_done               TIMESTAMPTZ,
    status                  TEXT NOT NULL DEFAULT 'pending', -- pending, in_progress, completed
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_card_id, step_number)
);

CREATE INDEX IF NOT EXISTS idx_process_step_jc ON job_card_process_step(job_card_id);

-- Job card output (Section 5: FG output, RM consumed, losses)
CREATE TABLE IF NOT EXISTS job_card_output (
    output_id               SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id) UNIQUE,
    fg_expected_units       INT,
    fg_actual_units         INT,
    fg_expected_kg          NUMERIC(15,3),
    fg_actual_kg            NUMERIC(15,3),
    rm_consumed_kg          NUMERIC(15,3),
    process_loss_kg         NUMERIC(15,3) DEFAULT 0,
    net_output_kg           NUMERIC(15,3) DEFAULT 0,   -- fg_actual_kg + byproduct_total_kg
    yield_pct               NUMERIC(8,3) DEFAULT 0,    -- net_output_kg / rm_consumed_kg * 100
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Job card environment (Annexure C: environmental parameters)
CREATE TABLE IF NOT EXISTS job_card_environment (
    env_id                  SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    parameter_name          TEXT NOT NULL,                  -- brine_salinity, temp, humidity, fan_pct, rpm, gas, magnet
    value                   TEXT,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_env_jc ON job_card_environment(job_card_id);

-- Job card metal detection (Annexure A/B)
CREATE TABLE IF NOT EXISTS job_card_metal_detection (
    detection_id            SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    check_type              TEXT NOT NULL,                  -- pre_packaging, post_packaging
    fe_pass                 BOOLEAN,
    nfe_pass                BOOLEAN,
    ss_pass                 BOOLEAN,
    failed_units            INT DEFAULT 0,
    remarks                 TEXT,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metal_jc ON job_card_metal_detection(job_card_id);

-- Job card weight checks (Annexure B: 20-sample checks)
CREATE TABLE IF NOT EXISTS job_card_weight_check (
    check_id                SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    sample_number           INT NOT NULL,
    net_weight              NUMERIC(15,3),
    gross_weight            NUMERIC(15,3),
    leak_test_pass          BOOLEAN,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_weight_jc ON job_card_weight_check(job_card_id);

-- Job card loss reconciliation (Annexure D)
CREATE TABLE IF NOT EXISTS job_card_loss_reconciliation (
    recon_id                SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    loss_category           TEXT NOT NULL,                  -- sorting_rejection, roasting_loss, packaging_rejection, metal_detector, spillage, qc_sample
    budgeted_loss_pct       NUMERIC(5,3),
    budgeted_loss_kg        NUMERIC(15,3),
    actual_loss_kg          NUMERIC(15,3),
    variance_kg             NUMERIC(15,3),
    remarks                 TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_loss_recon_jc ON job_card_loss_reconciliation(job_card_id);

-- Job card remarks (Annexure E)
CREATE TABLE IF NOT EXISTS job_card_remarks (
    remark_id               SERIAL PRIMARY KEY,
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    remark_type             TEXT NOT NULL,                  -- observation, deviation, corrective_action
    content                 TEXT,
    recorded_by             TEXT,
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_remarks_jc ON job_card_remarks(job_card_id);

-- =========================================================================
-- INVENTORY & TRACKING
-- =========================================================================

-- Floor inventory (stock per floor location)
CREATE TABLE IF NOT EXISTS floor_inventory (
    inventory_id            SERIAL PRIMARY KEY,
    sku_name                TEXT NOT NULL,
    item_type               TEXT,                          -- rm, pm, wip, fg
    floor_location          TEXT NOT NULL,                  -- rm_store, pm_store, production_floor, fg_store
    quantity_kg             NUMERIC(15,3) NOT NULL DEFAULT 0,
    lot_number              TEXT,
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    last_updated            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (sku_name, floor_location, lot_number, entity)
);

CREATE INDEX IF NOT EXISTS idx_floor_inv_location ON floor_inventory(floor_location);
CREATE INDEX IF NOT EXISTS idx_floor_inv_sku ON floor_inventory(sku_name);
CREATE INDEX IF NOT EXISTS idx_floor_inv_entity ON floor_inventory(entity);

-- Floor movement audit trail
CREATE TABLE IF NOT EXISTS floor_movement (
    movement_id             SERIAL PRIMARY KEY,
    sku_name                TEXT NOT NULL,
    from_location           TEXT NOT NULL,
    to_location             TEXT NOT NULL,
    quantity_kg             NUMERIC(15,3) NOT NULL,
    reason                  TEXT,                          -- production, return, receipt, dispatch
    job_card_id             INT REFERENCES job_card(job_card_id),
    scanned_qr_codes        TEXT[],                        -- array of po_box.box_id
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    moved_by                TEXT,
    moved_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_floor_move_jc ON floor_movement(job_card_id);
CREATE INDEX IF NOT EXISTS idx_floor_move_date ON floor_movement(moved_at);

-- Off-grade inventory
CREATE TABLE IF NOT EXISTS offgrade_inventory (
    offgrade_id             SERIAL PRIMARY KEY,
    source_product          TEXT NOT NULL,                  -- which FG produced this off-grade
    item_group              TEXT,                           -- cashew, almond, dates, etc.
    category                TEXT,                           -- broken, undersized, discolored, etc.
    grade                   TEXT,                           -- A, B, C
    available_qty_kg        NUMERIC(15,3) NOT NULL DEFAULT 0,
    production_date         DATE,
    expiry_date             DATE,
    job_card_id             INT REFERENCES job_card(job_card_id),
    status                  TEXT NOT NULL DEFAULT 'available', -- available, reserved, consumed, expired
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_offgrade_status ON offgrade_inventory(status);
CREATE INDEX IF NOT EXISTS idx_offgrade_group ON offgrade_inventory(item_group);

-- Off-grade reuse rules
CREATE TABLE IF NOT EXISTS offgrade_reuse_rule (
    rule_id                 SERIAL PRIMARY KEY,
    source_item_group       TEXT NOT NULL,
    target_item_group       TEXT NOT NULL,
    max_substitution_pct    NUMERIC(5,3) NOT NULL,         -- max % of RM that can be off-grade
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_item_group, target_item_group)
);

-- Off-grade consumption tracking
CREATE TABLE IF NOT EXISTS offgrade_consumption (
    consumption_id          SERIAL PRIMARY KEY,
    offgrade_id             INT NOT NULL REFERENCES offgrade_inventory(offgrade_id),
    job_card_id             INT NOT NULL REFERENCES job_card(job_card_id),
    qty_used_kg             NUMERIC(15,3) NOT NULL,
    consumed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_offgrade_cons_jc ON offgrade_consumption(job_card_id);

-- =========================================================================
-- ANALYTICS & SUPPORT
-- =========================================================================

-- Process loss records (auto-created from job card output)
CREATE TABLE IF NOT EXISTS process_loss (
    loss_id                 SERIAL PRIMARY KEY,
    job_card_id             INT REFERENCES job_card(job_card_id),
    product_name            TEXT NOT NULL,
    item_group              TEXT,
    machine_name            TEXT,
    stage                   TEXT,
    loss_kg                 NUMERIC(15,3) NOT NULL,
    loss_pct                NUMERIC(5,3),
    loss_category           TEXT,                          -- sorting, roasting, packaging, metal_detection, spillage
    batch_number            TEXT,
    production_date         DATE,
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_process_loss_product ON process_loss(product_name);
CREATE INDEX IF NOT EXISTS idx_process_loss_date ON process_loss(production_date);
CREATE INDEX IF NOT EXISTS idx_process_loss_entity ON process_loss(entity);

-- Quality inspection records
CREATE TABLE IF NOT EXISTS quality_inspection (
    inspection_id           SERIAL PRIMARY KEY,
    job_card_id             INT REFERENCES job_card(job_card_id),
    inspection_type         TEXT NOT NULL,                  -- in_process, final, metal_detection
    checkpoint              TEXT,                           -- stage name or specific check
    result                  TEXT NOT NULL,                  -- pass, fail, conditional
    notes                   TEXT,
    inspector_name          TEXT,
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    inspected_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qi_jc ON quality_inspection(job_card_id);
CREATE INDEX IF NOT EXISTS idx_qi_result ON quality_inspection(result);

-- Yield summary (computed periodically)
CREATE TABLE IF NOT EXISTS yield_summary (
    yield_id                SERIAL PRIMARY KEY,
    product_name            TEXT NOT NULL,
    item_group              TEXT,
    period                  TEXT NOT NULL,                  -- "2026-04", "2026-W14", etc.
    total_input_kg          NUMERIC(15,3) NOT NULL,
    total_output_kg         NUMERIC(15,3) NOT NULL,
    yield_pct               NUMERIC(5,3),
    total_loss_kg           NUMERIC(15,3),
    total_offgrade_kg       NUMERIC(15,3),
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yield_product ON yield_summary(product_name);
CREATE INDEX IF NOT EXISTS idx_yield_period ON yield_summary(period);

-- Purchase indents (raised when MRP detects shortage)
CREATE TABLE IF NOT EXISTS purchase_indent (
    indent_id               SERIAL PRIMARY KEY,
    indent_number           TEXT NOT NULL UNIQUE,           -- IND-20260401-001
    material_sku_name       TEXT NOT NULL,
    required_qty_kg         NUMERIC(15,3) NOT NULL,
    required_by_date        DATE,
    priority                INT DEFAULT 5,
    plan_line_id            INT REFERENCES production_plan_line(plan_line_id),
    po_reference            TEXT,                          -- linked PO number when created
    status                  TEXT NOT NULL DEFAULT 'raised', -- raised, acknowledged, po_created, received, cancelled
    acknowledged_by         TEXT,
    acknowledged_at         TIMESTAMPTZ,
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_indent_status ON purchase_indent(status);
CREATE INDEX IF NOT EXISTS idx_indent_entity ON purchase_indent(entity);

-- Store alerts (notifications for teams)
CREATE TABLE IF NOT EXISTS store_alert (
    alert_id                SERIAL PRIMARY KEY,
    alert_type              TEXT NOT NULL,                  -- material_shortage, indent_raised, material_received, force_unlock, anomaly, plan_ready
    target_team             TEXT NOT NULL,                  -- purchase, stores, production, qc
    message                 TEXT NOT NULL,
    related_id              INT,                           -- generic FK to related record
    related_type            TEXT,                           -- fulfillment, indent, job_card, plan
    is_read                 BOOLEAN DEFAULT FALSE,
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alert_team ON store_alert(target_team);
CREATE INDEX IF NOT EXISTS idx_alert_read ON store_alert(is_read);
CREATE INDEX IF NOT EXISTS idx_alert_entity ON store_alert(entity);

-- AI recommendation log (Claude interactions)
CREATE TABLE IF NOT EXISTS ai_recommendation (
    recommendation_id       SERIAL PRIMARY KEY,
    recommendation_type     TEXT NOT NULL,                  -- daily_plan, weekly_plan, full_plan, revision, loss_anomaly, offgrade_reuse, forecast
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    prompt_text             TEXT,
    response_text           TEXT,
    response_json           JSONB,
    tokens_used             INT,
    latency_ms              INT,
    model_used              TEXT,
    status                  TEXT NOT NULL DEFAULT 'generated', -- generated, accepted, rejected
    feedback                TEXT,
    plan_id                 INT REFERENCES production_plan(plan_id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_rec_type ON ai_recommendation(recommendation_type);
CREATE INDEX IF NOT EXISTS idx_ai_rec_status ON ai_recommendation(status);
CREATE INDEX IF NOT EXISTS idx_ai_rec_entity ON ai_recommendation(entity);

-- =========================================================================
-- DAY-END BALANCE SCAN & RECONCILIATION
-- =========================================================================

-- Day-end balance scan header (one per floor per day)
CREATE TABLE IF NOT EXISTS day_end_balance_scan (
    scan_id                 SERIAL PRIMARY KEY,
    floor_location          TEXT NOT NULL,
    scan_date               DATE NOT NULL,
    submitted_by            TEXT,
    submitted_at            TIMESTAMPTZ,
    reviewed_by             TEXT,
    reviewed_at             TIMESTAMPTZ,
    total_system_qty        NUMERIC(15,3),
    total_scanned_qty       NUMERIC(15,3),
    total_variance          NUMERIC(15,3),
    status                  TEXT NOT NULL DEFAULT 'pending', -- pending, submitted, variance_flagged, reconciled
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (floor_location, scan_date, entity)
);

CREATE INDEX IF NOT EXISTS idx_balance_scan_date ON day_end_balance_scan(scan_date);
CREATE INDEX IF NOT EXISTS idx_balance_scan_status ON day_end_balance_scan(status);

-- Day-end balance scan line items
CREATE TABLE IF NOT EXISTS day_end_balance_scan_line (
    scan_line_id            SERIAL PRIMARY KEY,
    scan_id                 INT NOT NULL REFERENCES day_end_balance_scan(scan_id),
    sku_name                TEXT NOT NULL,
    item_type               TEXT,
    system_qty_kg           NUMERIC(15,3),
    scanned_qty_kg          NUMERIC(15,3),
    variance_kg             NUMERIC(15,3),
    variance_pct            NUMERIC(5,3),
    scanned_box_ids         TEXT[],
    variance_reason         TEXT,
    corrective_action       TEXT,
    status                  TEXT NOT NULL DEFAULT 'ok'  -- ok, variance_detected, reconciled
);

CREATE INDEX IF NOT EXISTS idx_balance_scan_line_scan ON day_end_balance_scan_line(scan_id);

-- =========================================================================
-- INTERNAL DISCREPANCY TRACKING
-- =========================================================================

-- Discrepancy reports (RM grade mismatch, QC failure, machine breakdown, etc.)
CREATE TABLE IF NOT EXISTS discrepancy_report (
    discrepancy_id          SERIAL PRIMARY KEY,
    discrepancy_type        TEXT NOT NULL,                  -- rm_grade_mismatch, rm_qc_failure, rm_expired, machine_breakdown, contamination, short_delivery
    severity                TEXT NOT NULL DEFAULT 'major',  -- critical, major, minor
    affected_material       TEXT,
    affected_machine_id     INT REFERENCES machine(machine_id),
    affected_job_card_ids   INT[],
    affected_plan_line_ids  INT[],
    details                 TEXT,
    total_affected_qty_kg   NUMERIC(15,3),
    customer_impact         TEXT,
    resolution_type         TEXT,                           -- material_substituted, machine_rescheduled, deferred, cancelled_replanned, proceed_with_deviation
    resolution_details      TEXT,
    reported_by             TEXT,
    reported_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_by             TEXT,
    resolved_at             TIMESTAMPTZ,
    status                  TEXT NOT NULL DEFAULT 'open',   -- open, investigating, resolved, closed
    entity                  TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discrepancy_status ON discrepancy_report(status);
CREATE INDEX IF NOT EXISTS idx_discrepancy_entity ON discrepancy_report(entity);

-- Floor stock adjustments: manual stock entries for materials lying on production floors
CREATE TABLE IF NOT EXISTS fulfillment_floor_stock (
    floor_stock_id          SERIAL PRIMARY KEY,
    fulfillment_id          INT NOT NULL REFERENCES so_fulfillment(fulfillment_id),
    material_sku_name       TEXT NOT NULL,
    item_type               TEXT DEFAULT 'pm',              -- rm, pm
    quantity_kg             NUMERIC(15,3) NOT NULL,
    unit                    TEXT NOT NULL DEFAULT 'KG',      -- KG or NOS
    floor_location          TEXT NOT NULL,                   -- exact floor or warehouse name
    added_by                TEXT,
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_floor_stock_fulfillment ON fulfillment_floor_stock(fulfillment_id);
CREATE INDEX IF NOT EXISTS idx_floor_stock_material ON fulfillment_floor_stock(material_sku_name);

-- =====================================================================
-- INVENTORY BATCH LEDGER — single source of truth for all batch records
-- =====================================================================

CREATE TABLE IF NOT EXISTS inventory_batch (
    batch_id            TEXT PRIMARY KEY,
    sku_name            TEXT NOT NULL,
    item_type           TEXT,                              -- rm/pm/fg/wip
    transaction_no      TEXT,                              -- FK po_header (NULL for legacy/production)
    lot_number          TEXT,
    source              TEXT NOT NULL DEFAULT 'INWARD',    -- INWARD / STOCK_TAKE / PRODUCTION / RETURN
    inward_date         DATE NOT NULL,                     -- for FIFO ordering
    manufacturing_date  DATE,
    expiry_date         DATE,
    original_qty_kg     NUMERIC(15,3) NOT NULL,
    current_qty_kg      NUMERIC(15,3) NOT NULL,
    warehouse_id        TEXT,
    floor_id            TEXT,                               -- current floor location
    status              TEXT NOT NULL DEFAULT 'AVAILABLE',  -- AVAILABLE/BLOCKED/ISSUED/IN_TRANSIT/INTERNAL_HOLD/FLAGGED
    blocked_for_so_id   INT,                               -- FK so_header when BLOCKED
    blocked_by          TEXT,
    blocked_at          TIMESTAMPTZ,
    block_reason        TEXT,
    flag_reason         TEXT,                               -- when FLAGGED (skipped in FIFO)
    flag_detail         TEXT,
    ownership           TEXT NOT NULL DEFAULT 'FLOOR',      -- FLOOR or STORES (for store-in-floor)
    entity              TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inv_batch_sku_status ON inventory_batch(sku_name, status, entity);
CREATE INDEX IF NOT EXISTS idx_inv_batch_status ON inventory_batch(status);
CREATE INDEX IF NOT EXISTS idx_inv_batch_blocked_so ON inventory_batch(blocked_for_so_id);
CREATE INDEX IF NOT EXISTS idx_inv_batch_fifo ON inventory_batch(inward_date);
CREATE INDEX IF NOT EXISTS idx_inv_batch_floor ON inventory_batch(floor_id);
CREATE INDEX IF NOT EXISTS idx_inv_batch_entity ON inventory_batch(entity);

-- Immutable append-only audit trail for every batch change
CREATE TABLE IF NOT EXISTS inventory_event_log (
    event_id        SERIAL PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,      -- CREATED/MOVED/BLOCKED/UNBLOCKED/ISSUED/RETURNED/FLAGGED/ADJUSTED/OVERRIDE
    from_status     TEXT,
    to_status       TEXT,
    from_location   TEXT,               -- warehouse_id:floor_id
    to_location     TEXT,
    quantity_kg     NUMERIC(15,3),
    reference_type  TEXT,               -- job_card/so/indent/transfer/stock_take
    reference_id    INT,
    so_id           INT,
    performed_by    TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inv_event_batch ON inventory_event_log(batch_id);
CREATE INDEX IF NOT EXISTS idx_inv_event_type ON inventory_event_log(event_type);

-- Archive of all block/unblock/override events per batch
CREATE TABLE IF NOT EXISTS batch_block_history (
    id              SERIAL PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    action          TEXT NOT NULL,      -- BLOCKED/UNBLOCKED/OVERRIDDEN/REASSIGNED
    so_id           INT,
    blocked_by      TEXT,
    override_by     TEXT,
    override_note   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_block_hist_batch ON batch_block_history(batch_id);

-- Internal issue notes for non-SO floor transfers with approval
CREATE TABLE IF NOT EXISTS internal_issue_note (
    note_id             SERIAL PRIMARY KEY,
    note_number         TEXT NOT NULL UNIQUE,    -- IIN-YYYYMMDD-SEQ
    sku_name            TEXT NOT NULL,
    batch_id            TEXT,                    -- FK inventory_batch
    quantity_kg         NUMERIC(15,3) NOT NULL,
    source_warehouse    TEXT,
    source_floor        TEXT,
    destination_floor   TEXT NOT NULL,
    purpose             TEXT NOT NULL,           -- sorting/grading/reprocessing/qc/other
    requested_by        TEXT NOT NULL,
    approved_by         TEXT,
    approved_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/completed
    entity              TEXT CHECK (entity IN ('cfpl', 'cdpl')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_iin_status ON internal_issue_note(status);
CREATE INDEX IF NOT EXISTS idx_iin_entity ON internal_issue_note(entity);
