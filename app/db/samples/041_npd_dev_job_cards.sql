-- 041_npd_dev_job_cards.sql
-- Standalone NPD development job cards (spec: NPD product-development track).
--
-- Unlike the requisition-bound NPD draft BOM (npd_draft_boms, which hangs off a
-- sample requisition and is gated by that requisition's BH approval), these are
-- pure R&D: a development job card is created DIRECTLY (no sample requisition),
-- the trial recipe is authored in npd_dev_job_card_lines, and CLOSING the card
-- records the trial output AND promotes the recipe into a live bom_header +
-- bom_line so the new product becomes real. The sample-issuance lifecycle is a
-- separate process — these never touch sample_requisitions.
--
-- House numbering: NPDJC-YYYYMMDD-NNNN via seq_npd_dev_jc (the _gen_id pattern).

CREATE TABLE IF NOT EXISTS npd_dev_job_cards (
    id                  SERIAL PRIMARY KEY,
    dev_jc_number       TEXT UNIQUE NOT NULL,              -- NPDJC-YYYYMMDD-NNNN
    title               TEXT NOT NULL,
    description         TEXT,
    warehouse           TEXT,                              -- optional R&D location
    base_bom_id         INT REFERENCES bom_header(bom_id), -- optional recipe to start from
    fg_sku_id           INT REFERENCES all_sku(sku_id),    -- target product (optional)
    fg_sku_name         TEXT,
    target_qty          NUMERIC(15,3),
    uom                 TEXT,
    status              TEXT NOT NULL DEFAULT 'DRAFT'
                        CHECK (status IN ('DRAFT', 'IN_DEVELOPMENT', 'CLOSED', 'CANCELLED')),
    -- Trial output, captured at closure.
    output_qty          NUMERIC(15,3),
    output_uom          TEXT,
    yield_pct           NUMERIC(7,3),
    output_notes        TEXT,
    -- The live BOM minted when the recipe is promoted on closure.
    promoted_bom_id     INT REFERENCES bom_header(bom_id),
    cancellation_reason TEXT,
    created_by          INT REFERENCES auth_user(user_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_by          INT REFERENCES auth_user(user_id),
    started_at          TIMESTAMPTZ,
    closed_by           INT REFERENCES auth_user(user_id),
    closed_at           TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_npd_dev_jc_status  ON npd_dev_job_cards(status);
CREATE INDEX IF NOT EXISTS idx_npd_dev_jc_created ON npd_dev_job_cards(created_at);
CREATE SEQUENCE IF NOT EXISTS seq_npd_dev_jc START 1;

-- Trial recipe lines (the BOM under development). sku_id is nullable for the
-- same reason as npd_draft_bom_lines: clone-from-base lines are name-based
-- (bom_line carries no sku_id).
CREATE TABLE IF NOT EXISTS npd_dev_job_card_lines (
    id              SERIAL PRIMARY KEY,
    dev_jc_id       INT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    sku_id          INT REFERENCES all_sku(sku_id),
    sku_name        TEXT NOT NULL,
    qty             NUMERIC(15,3) NOT NULL CHECK (qty >= 0),
    uom             TEXT NOT NULL,
    item_type       TEXT CHECK (item_type IN ('rm', 'pm')),
    line_order      INT NOT NULL DEFAULT 0,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_npd_dev_jc_lines ON npd_dev_job_card_lines(dev_jc_id);
