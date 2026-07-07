-- 053_requisition_quantity.sql  (requested quantity on the requisition)
-- A free numeric quantity the requester can put on the request (e.g. how much of
-- the new product to develop / sample). Nullable float; no UOM coupling here —
-- the article lines carry their own UOM in the issuance flows. Idempotent.
ALTER TABLE sample_requisitions
    ADD COLUMN IF NOT EXISTS quantity NUMERIC(15,3);
