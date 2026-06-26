-- =========================================================================
-- Migration 017: Job Card v2 schema (parallel to v1, follows the v2 pattern
-- established by so_fulfillment_v2 and production_plan_v2).
--
-- Why a parallel v2 instead of extending v1:
--   • v1 job_card was tied to production_order and carried legacy fields
--     (store_allocation_status, batch_number FK conventions, etc.) that
--     don't apply to the new plan-approval-driven flow.
--   • Splitting cleanly lets v1 continue to serve any in-flight v1 plans
--     and lets v2 readers (dashboards, floor app) opt into the new shape
--     without back-compat shims.
--
-- v2 tables created here:
--   • job_card_v2                — 8-digit app-supplied BIGINT PK
--   • job_card_shift_log_v2      — append-only per-shift time segments
--   • job_card_output_v2         — actual qty + yield per JC
--   • job_card_partial_dispatch_v2 — stage-to-stage handoff audit
--   • job_card_rm_indent_v2      — RM consumption per stage
--   • job_card_pm_indent_v2      — PM consumption per stage
--   • job_card_sign_off_v2       — per-role sign-offs gating close
--
-- The legacy job_card additions from migration 016 (plan_id /
-- plan_line_id / plan_step_id / input_kind / output_kind / uom +
-- job_card_shift_log) are NO LONGER USED by the v2 service. They stay in
-- place for any pre-016 callers but receive no writes from v2.
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- -----------------------------------------------------------------------
-- 1. job_card_v2 — the JC ledger
--
-- PK is app-supplied (8-digit time-based BIGINT, same pattern as
-- so_fulfillment_v2.so_fulfillment_id and production_plan_v2.plan_id).
-- Caller uses new_short_time_id() + insert_with_pk_retry to handle
-- collisions on a same-millisecond burst.
--
-- Lineage: plan_id / plan_line_id / plan_step_id are NOT NULL because v2
-- JCs are always derived from an approved plan — no orphan job cards.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_v2 (
    job_card_id        BIGINT PRIMARY KEY,
    job_card_number    TEXT NOT NULL UNIQUE,                       -- "PLAN-{plan_id}-L{line}-S{step}"
    -- Lineage
    plan_id            BIGINT NOT NULL
                       REFERENCES production_plan_v2(plan_id)      ON DELETE RESTRICT,
    plan_line_id       BIGINT NOT NULL
                       REFERENCES production_plan_line_v2(plan_line_id) ON DELETE RESTRICT,
    plan_step_id       BIGINT NOT NULL
                       REFERENCES production_plan_step_v2(step_id) ON DELETE RESTRICT,
    bom_id             INT REFERENCES bom_header(bom_id),
    -- Stage shape
    step_number        INT  NOT NULL,                              -- 1..N within the line
    process_name       TEXT NOT NULL,
    stage              TEXT NOT NULL,
    -- Product (denormalized for cheap reads)
    fg_sku_name        TEXT NOT NULL,
    customer_name      TEXT,
    batch_number       TEXT NOT NULL,                              -- "P{plan_id}-L{line}-S{step}"
    planned_qty_kg     NUMERIC(15,3) NOT NULL CHECK (planned_qty_kg > 0),
    planned_qty_units  NUMERIC(15,3),
    uom                TEXT,                                       -- KGS / GMS / NOS / PCS / ROLL / SETS / BUNDLE / LTRS
    -- Material flow context (the stage-chain semantics from the spec)
    input_kind         TEXT NOT NULL CHECK (input_kind  IN ('RM','SFG','WIP')),
    output_kind        TEXT NOT NULL CHECK (output_kind IN ('SFG','WIP','FG')),
    -- Location (mirrored from the plan step + plan header)
    factory            TEXT NOT NULL CHECK (factory IN ('W-202','A-185')),
    floor              TEXT,
    entity             TEXT NOT NULL CHECK (entity   IN ('cfpl','cdpl')),
    -- Machine + team
    machine_id         INT  REFERENCES machine(machine_id),
    assigned_to_team_leader TEXT,
    team_members       TEXT[],
    -- Locking + lifecycle
    is_locked          BOOLEAN NOT NULL DEFAULT TRUE,
    locked_reason      TEXT,                                       -- 'awaiting_previous_stage' / 'material_pending' / NULL
    force_unlocked     BOOLEAN DEFAULT FALSE,
    force_unlock_by    TEXT,
    force_unlock_reason TEXT,
    force_unlock_at    TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'locked'
                       CHECK (status IN ('locked','unlocked','assigned',
                                         'material_received','in_progress',
                                         'completed','closed','cancelled')),
    -- Time rollup (sum of closed segments in job_card_shift_log_v2)
    start_time         TIMESTAMPTZ,
    end_time           TIMESTAMPTZ,
    total_time_min     NUMERIC(10,2) NOT NULL DEFAULT 0,
    -- Stage chain pointers
    prev_job_card_id   BIGINT REFERENCES job_card_v2(job_card_id),
    next_job_card_id   BIGINT REFERENCES job_card_v2(job_card_id),
    carried_qty_kg     NUMERIC(15,3) NOT NULL DEFAULT 0,           -- received from prev stage
    dispatched_to_next_kg NUMERIC(15,3) NOT NULL DEFAULT 0,        -- pushed to next stage
    -- Audit + soft-delete
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ,
    updated_by         TEXT,
    deleted_at         TIMESTAMPTZ,
    deleted_by         TEXT,
    cancellation_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_jc_v2_plan        ON job_card_v2(plan_id);
CREATE INDEX IF NOT EXISTS idx_jc_v2_plan_line   ON job_card_v2(plan_line_id);
CREATE INDEX IF NOT EXISTS idx_jc_v2_status      ON job_card_v2(status);
CREATE INDEX IF NOT EXISTS idx_jc_v2_floor       ON job_card_v2(floor) WHERE floor IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jc_v2_factory     ON job_card_v2(factory);
CREATE INDEX IF NOT EXISTS idx_jc_v2_entity      ON job_card_v2(entity);
CREATE INDEX IF NOT EXISTS idx_jc_v2_chain_prev  ON job_card_v2(prev_job_card_id) WHERE prev_job_card_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jc_v2_chain_next  ON job_card_v2(next_job_card_id) WHERE next_job_card_id IS NOT NULL;
-- Partial index to power the "open JCs on this floor today" hot query.
CREATE INDEX IF NOT EXISTS idx_jc_v2_floor_open
    ON job_card_v2(floor, factory)
    WHERE deleted_at IS NULL AND status NOT IN ('closed','cancelled');

COMMENT ON TABLE  job_card_v2 IS
    'v2 job card. One row per (plan line × plan step). PK is application-supplied '
    '8-digit time-based BIGINT. plan_id / plan_line_id / plan_step_id are NOT NULL '
    'because v2 JCs are always derived from an approved plan.';
COMMENT ON COLUMN job_card_v2.input_kind  IS
    'Material kind received at this stage. First stage in the chain = RM.';
COMMENT ON COLUMN job_card_v2.output_kind IS
    'Material kind produced by this stage. Last stage in the chain = FG; '
    'intermediate stages produce WIP that becomes the next stage''s SFG input.';

-- -----------------------------------------------------------------------
-- 2. job_card_shift_log_v2 — multi-shift, multi-day time capture
--
-- A single JC may run across A/B/C shifts and across days. Each active
-- shift adds one row. open segment = end_at IS NULL. job_card_v2.total_time_min
-- is the sum of (end_at - start_at) - paused_minutes across closed segments.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_shift_log_v2 (
    log_id          BIGSERIAL PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    shift           TEXT NOT NULL CHECK (shift IN ('A','B','C','general')),
    shift_date      DATE NOT NULL,
    start_at        TIMESTAMPTZ NOT NULL,
    end_at          TIMESTAMPTZ,
    paused_minutes  INT NOT NULL DEFAULT 0 CHECK (paused_minutes >= 0),
    operator_name   TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_at IS NULL OR end_at >= start_at)
);

CREATE INDEX IF NOT EXISTS idx_jcsl_v2_jc   ON job_card_shift_log_v2(job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcsl_v2_open ON job_card_shift_log_v2(job_card_id)
    WHERE end_at IS NULL;
-- One open segment per JC at any time. Enforced via partial unique index:
-- when end_at IS NULL, the (job_card_id) value must be unique. Closed
-- segments are excluded so a JC can have many of them.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jcsl_v2_one_open
    ON job_card_shift_log_v2(job_card_id)
    WHERE end_at IS NULL;

-- -----------------------------------------------------------------------
-- 3. job_card_output_v2 — qty actual + yield capture
--
-- Recorded when a stage completes. RM consumed + FG/WIP produced + yield%.
-- Last stage's row carries the actual FG yield; intermediate stages carry
-- WIP / SFG yield for traceability.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_output_v2 (
    output_id       BIGSERIAL PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    rm_consumed_kg  NUMERIC(15,3) NOT NULL DEFAULT 0,
    output_qty_kg   NUMERIC(15,3) NOT NULL CHECK (output_qty_kg >= 0),
    output_qty_units NUMERIC(15,3),
    output_kind     TEXT NOT NULL CHECK (output_kind IN ('SFG','WIP','FG')),
    uom             TEXT,
    yield_pct       NUMERIC(6,3),                                  -- output / rm_consumed × 100
    notes           TEXT,
    recorded_by     TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jco_v2_jc ON job_card_output_v2(job_card_id);

-- -----------------------------------------------------------------------
-- 4. job_card_partial_dispatch_v2 — stage-to-stage handoff audit
--
-- One row per dispatch event from stage N to stage N+1. Sum of qty_kg
-- equals the parent JC's dispatched_to_next_kg. Provides a paper trail
-- for partial WIP transfers.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_partial_dispatch_v2 (
    dispatch_id     BIGSERIAL PRIMARY KEY,
    from_job_card_id BIGINT NOT NULL
                     REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    to_job_card_id   BIGINT NOT NULL
                     REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    qty_kg          NUMERIC(15,3) NOT NULL CHECK (qty_kg > 0),
    qty_units       NUMERIC(15,3),
    dispatched_by   TEXT,
    dispatched_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_jcpd_v2_from ON job_card_partial_dispatch_v2(from_job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcpd_v2_to   ON job_card_partial_dispatch_v2(to_job_card_id);

-- -----------------------------------------------------------------------
-- 5. job_card_rm_indent_v2 — RM consumption per stage
--
-- Same shape as v1 job_card_rm_indent but FK points at job_card_v2 and
-- uom is constrained to the values valid for raw materials.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_rm_indent_v2 (
    rm_indent_id    BIGSERIAL PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    material_sku_name TEXT NOT NULL,
    uom             TEXT NOT NULL CHECK (uom IN ('KGS','GMS','LTRS','NOS')),
    reqd_qty        NUMERIC(15,3) NOT NULL CHECK (reqd_qty > 0),
    loss_pct        NUMERIC(5,3) DEFAULT 0,
    gross_qty       NUMERIC(15,3) NOT NULL,
    issued_qty      NUMERIC(15,3) NOT NULL DEFAULT 0,
    batch_no        TEXT,
    godown          TEXT,
    scanned_box_ids TEXT[],
    variance        NUMERIC(15,3),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','partial','fulfilled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rm_v2_jc ON job_card_rm_indent_v2(job_card_id);

-- -----------------------------------------------------------------------
-- 6. job_card_pm_indent_v2 — PM consumption per stage
--
-- Per the bill-of-units sheet, PM is valid in KGS / NOS / ROLL / SETS /
-- PCS / BUNDLE. The CHECK enforces that subset.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_pm_indent_v2 (
    pm_indent_id    BIGSERIAL PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    material_sku_name TEXT NOT NULL,
    uom             TEXT NOT NULL CHECK (uom IN ('KGS','NOS','ROLL','SETS','PCS','BUNDLE')),
    reqd_qty        NUMERIC(15,3) NOT NULL CHECK (reqd_qty > 0),
    loss_pct        NUMERIC(5,3) DEFAULT 0,
    gross_qty       NUMERIC(15,3) NOT NULL,
    issued_qty      NUMERIC(15,3) NOT NULL DEFAULT 0,
    batch_no        TEXT,
    godown          TEXT,
    scanned_box_ids TEXT[],
    variance        NUMERIC(15,3),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','partial','fulfilled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pm_v2_jc ON job_card_pm_indent_v2(job_card_id);

-- -----------------------------------------------------------------------
-- 7. job_card_sign_off_v2 — per-role sign-offs that gate close
--
-- The close endpoint refuses to flip status='closed' until every required
-- sign-off role has a row here. Configurable per process (team_leader,
-- qc_inspector, floor_manager, etc.).
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_sign_off_v2 (
    sign_off_id     BIGSERIAL PRIMARY KEY,
    job_card_id     BIGINT NOT NULL
                    REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    role            TEXT NOT NULL,                                 -- 'team_leader','qc_inspector','floor_manager'
    signed_by       TEXT NOT NULL,
    signed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes           TEXT,
    UNIQUE (job_card_id, role)                                     -- one sign-off per role per JC
);

CREATE INDEX IF NOT EXISTS idx_jcso_v2_jc ON job_card_sign_off_v2(job_card_id);

-- -----------------------------------------------------------------------
-- updated_at touch trigger — same fn_touch_updated_at used by other v2 tables.
-- -----------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_jc_v2_updated_at ON job_card_v2;
CREATE TRIGGER trg_jc_v2_updated_at
    BEFORE UPDATE ON job_card_v2
    FOR EACH ROW
    EXECUTE FUNCTION fn_touch_updated_at();

COMMIT;

-- ── Verification ────────────────────────────────────────────────────────
-- All seven tables should report present.
SELECT table_name
FROM   information_schema.tables
WHERE  table_name LIKE 'job_card_%_v2' OR table_name = 'job_card_v2'
ORDER  BY table_name;

-- Key columns on job_card_v2:
SELECT column_name, data_type, is_nullable
FROM   information_schema.columns
WHERE  table_name = 'job_card_v2'
  AND  column_name IN ('job_card_id','plan_id','plan_line_id','plan_step_id',
                       'input_kind','output_kind','uom','floor','factory',
                       'prev_job_card_id','next_job_card_id','status')
ORDER  BY ordinal_position;
