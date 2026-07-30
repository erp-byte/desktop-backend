-- 079_rtv_cferp_write_columns.sql  (OUT-OF-BAND — apply to AWS RDS warehouse_db only)
--
-- CFERP Phase-2: let the CFERP customer-returns module WRITE the shared live
-- legacy _rtv_* tables (IMS's production data) as a peer of IMS. These three
-- columns are the only ones CFERP's line/approval model needs that the legacy
-- _rtv_* schema lacks; all are nullable so IMS (which never selects them) is
-- unaffected.
--
-- NOT wired into scripts/migrate.py: _rtv_* exists only on the RDS prod DB, so
-- this must be applied out-of-band to RDS (like 067/068). Additive + idempotent.
ALTER TABLE cfpl_rtv_header ADD COLUMN IF NOT EXISTS approval_remark TEXT;
ALTER TABLE cdpl_rtv_header ADD COLUMN IF NOT EXISTS approval_remark TEXT;
ALTER TABLE cfpl_rtv_lines  ADD COLUMN IF NOT EXISTS sale_group TEXT;
ALTER TABLE cfpl_rtv_lines  ADD COLUMN IF NOT EXISTS conversion TEXT;
ALTER TABLE cdpl_rtv_lines  ADD COLUMN IF NOT EXISTS sale_group TEXT;
ALTER TABLE cdpl_rtv_lines  ADD COLUMN IF NOT EXISTS conversion TEXT;
