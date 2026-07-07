-- 051: the requested NPD target article name, captured ON the requisition.
-- Lets a business head raise an NPD request that NAMES the new product up front;
-- the NPD team prefills the draft BOM's fg_sku_name from it when they open it
-- into development. Recipe authoring + promotion stay NPD-gated.
ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS npd_target_name TEXT;
