-- Migration 001: Job card chain + partial dispatch
--
-- Adds the columns and table that the production module's code
-- (job_card_engine.create_job_cards, dispatch_partial_to_next_stage,
-- and the /job-cards/*/dispatch-log, /orders/*/job-card-chain endpoints)
-- already references but which were never added to the schema.
--
-- Idempotent. Safe to re-run.

-- ---------------------------------------------------------------------------
-- job_card: chaining + carried / dispatched quantities
-- ---------------------------------------------------------------------------
ALTER TABLE job_card
    ADD COLUMN IF NOT EXISTS next_job_card_id INT REFERENCES job_card(job_card_id);
ALTER TABLE job_card
    ADD COLUMN IF NOT EXISTS prev_job_card_id INT REFERENCES job_card(job_card_id);
ALTER TABLE job_card
    ADD COLUMN IF NOT EXISTS carried_qty_kg NUMERIC(15,3) NOT NULL DEFAULT 0;
ALTER TABLE job_card
    ADD COLUMN IF NOT EXISTS dispatched_to_next_kg NUMERIC(15,3) NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_job_card_next ON job_card(next_job_card_id);
CREATE INDEX IF NOT EXISTS idx_job_card_prev ON job_card(prev_job_card_id);

-- ---------------------------------------------------------------------------
-- Self-reference guards (CR-04): prevent A -> A chain corruption.
--
-- NOTE: these CHECK constraints only prevent 1-cycles (self-loops). They do
-- NOT prevent longer cycles (A -> B -> A), nor do they enforce that chained
-- JCs share the same prod_order_id / entity. The application code (see
-- job_card_engine.create_job_cards) is the single authoritative writer of
-- these columns and is responsible for cycle prevention and same-order /
-- same-entity invariants. Any future code path that rewires chains MUST
-- preserve these invariants. Full cycle detection requires a recursive CTE
-- and is intentionally out of scope for this migration.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_jc_no_self_chain'
    ) THEN
        ALTER TABLE job_card
            ADD CONSTRAINT chk_jc_no_self_chain
            CHECK (next_job_card_id IS NULL OR next_job_card_id <> job_card_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_jc_no_self_prev'
    ) THEN
        ALTER TABLE job_card
            ADD CONSTRAINT chk_jc_no_self_prev
            CHECK (prev_job_card_id IS NULL OR prev_job_card_id <> job_card_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- job_card_partial_dispatch: audit log for chunked handoffs between stages
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_card_partial_dispatch (
    dispatch_id        SERIAL PRIMARY KEY,
    from_job_card_id   INT NOT NULL REFERENCES job_card(job_card_id),
    to_job_card_id     INT NOT NULL REFERENCES job_card(job_card_id),
    qty_kg             NUMERIC(15,3) NOT NULL CHECK (qty_kg > 0),
    dispatched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_by      TEXT
);

CREATE INDEX IF NOT EXISTS idx_jcpd_from ON job_card_partial_dispatch(from_job_card_id);
CREATE INDEX IF NOT EXISTS idx_jcpd_to   ON job_card_partial_dispatch(to_job_card_id);
