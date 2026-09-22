-- ===========================================================================
-- 114_floor_requisition_box_unique.sql
-- A box goes out on ONE floor requisition. box_code becomes unique across every
-- request (113's primary key made it unique per request only). The scan checks
-- floor_requisition_box first and refuses a box already sent on any request;
-- this index makes that hold when two people scan the same box at once
-- (app/modules/floor_requisition/services/box_service.py).
-- It replaces 113's plain index on box_code, which it makes redundant.
--
-- Fails if a box is already on two requests. List them with:
--   SELECT box_code, array_agg(requisition_id) FROM floor_requisition_box
--    GROUP BY box_code HAVING count(*) > 1;
-- and remove the extra rows first (the X on the box in
-- Stores -> Production Indents -> Scan, while that request is Issued).
--
-- MUST follow 113. Idempotent.
-- ===========================================================================

CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_box_code
    ON floor_requisition_box (box_code);

DROP INDEX IF EXISTS idx_floor_requisition_box_code;
