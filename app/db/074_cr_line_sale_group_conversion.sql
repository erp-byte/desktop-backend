-- 074_cr_line_sale_group_conversion.sql
-- Customer-Returns line parity with the legacy RTV line model: the source line
-- carries `sale_group` (the sales grouping auto-filled from all_sku when an item
-- is picked) and a line-level `conversion` string. The initial 070 port dropped
-- both; add them back so CR line items match the legacy backend exactly.
-- Additive + idempotent (ADD COLUMN IF NOT EXISTS) — safe to re-run.

ALTER TABLE cfpl_customer_return_lines ADD COLUMN IF NOT EXISTS sale_group TEXT;
ALTER TABLE cfpl_customer_return_lines ADD COLUMN IF NOT EXISTS conversion TEXT;
ALTER TABLE cdpl_customer_return_lines ADD COLUMN IF NOT EXISTS sale_group TEXT;
ALTER TABLE cdpl_customer_return_lines ADD COLUMN IF NOT EXISTS conversion TEXT;
