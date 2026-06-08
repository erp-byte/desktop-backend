-- 043_recipe_line_ownership.sql  (NPD plan §3, §1)
-- Per-ingredient OWNERSHIP + off-master flags on BOTH NPD recipe-line tables:
-- the requisition-bound draft (npd_draft_bom_lines, §1/§3) and the standalone
-- dev job card (npd_dev_job_card_lines, §2). The accounting backbone (§1 Step A)
-- keys off these: ownership='CUSTOMER' and is_off_master=TRUE lines are recorded
-- for traceability / yield only and get NO inventory posting (we never owned the
-- stock). Defaults keep every existing row 'OWN' / not-off-master.

ALTER TABLE npd_draft_bom_lines
    ADD COLUMN IF NOT EXISTS ownership TEXT NOT NULL DEFAULT 'OWN'
        CHECK (ownership IN ('OWN', 'CUSTOMER')),
    ADD COLUMN IF NOT EXISTS is_off_master    BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS customer_lot_ref TEXT,
    ADD COLUMN IF NOT EXISTS received_qty     NUMERIC(15, 3);

ALTER TABLE npd_dev_job_card_lines
    ADD COLUMN IF NOT EXISTS ownership TEXT NOT NULL DEFAULT 'OWN'
        CHECK (ownership IN ('OWN', 'CUSTOMER')),
    ADD COLUMN IF NOT EXISTS is_off_master    BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS customer_lot_ref TEXT,
    ADD COLUMN IF NOT EXISTS received_qty     NUMERIC(15, 3);
