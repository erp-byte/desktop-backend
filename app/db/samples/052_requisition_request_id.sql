-- 052_requisition_request_id.sql  (8-digit BIGINT request id per requisition)
-- Every sample requisition holds a stable 8-digit numeric request id in addition
-- to the human SMP-YYYYMMDD-NNNN number. Implemented as a STORED generated column
-- derived from the serial PK (10000000 + id):
--   • computed for all existing rows when the column is added, and for every new
--     row automatically — no app/service change, no backfill, no collisions;
--   • unique because id is unique; always 8 digits while id < 89,999,999.
-- Idempotent (ADD COLUMN IF NOT EXISTS).
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS request_id BIGINT
    GENERATED ALWAYS AS (10000000 + id) STORED;
