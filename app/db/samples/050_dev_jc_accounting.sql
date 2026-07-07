-- 050: closure accounting fields for NPD development job cards.
-- Mirrors the production job card's Material Accounting Summary — RM consumed
-- (input) reconciled against FG output + wastage + extra give-away. Yield % is
-- derived (FG output / RM consumed) and stored in the existing yield_pct column.
ALTER TABLE npd_dev_job_cards ADD COLUMN IF NOT EXISTS rm_consumed_qty     NUMERIC(15,3);
ALTER TABLE npd_dev_job_cards ADD COLUMN IF NOT EXISTS wastage_qty         NUMERIC(15,3);
ALTER TABLE npd_dev_job_cards ADD COLUMN IF NOT EXISTS extra_give_away_qty NUMERIC(15,3);
