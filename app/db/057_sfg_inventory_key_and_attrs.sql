-- ===========================================================================
-- 057_sfg_inventory_key_and_attrs.sql  (SFG / Job-Card — structural tightening)
-- ---------------------------------------------------------------------------
-- Two follow-ups from the data-model review:
--
-- PT1 — stop overloading inventory_batch.sku_name with the SFG#### code.
--   Today WIP batches store the code in sku_name (the column that holds the
--   article NAME for rm/pm/fg), so the indexed name column mixes codes + names.
--   Add an explicit sfg_code column; WIP reads key on it. create_wip_batch then
--   stores sku_name = the SFG article NAME (uniform with rm/pm/fg) and the code
--   in sfg_code. Existing WIP rows (sku_name = code) are BACKFILLED sfg_code =
--   sku_name so the new sfg_code-keyed reads find them too (no data loss, no
--   read regression).
--
-- GAP B — make the catalogue ready for the two attributes that only exist in the
--   (absent) SFG_WIP_Item_List_FINAL source: va_article, primary_bu. Add the
--   columns now (NULL until that file is ingested) and surface them in sfg_master,
--   so dropping the FINAL file in later is a pure data load — no schema change.
--   NOTE: the 343-vs-489 SFG COUNT gap is NOT closable here — that source set is
--   not in the repo; this only prepares the columns.
--
-- Idempotent. Reversible via app/db/rollback/sfg_integration_down.sql.
-- (dep: 050-056; inventory_batch from production_schema; sfg_attributes from 056)
-- ===========================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- PT1: inventory_batch.sfg_code (canonical WIP key; no longer overload sku_name)
-- ---------------------------------------------------------------------------
ALTER TABLE inventory_batch ADD COLUMN IF NOT EXISTS sfg_code TEXT;
COMMENT ON COLUMN inventory_batch.sfg_code IS
    'SFG#### code for item_type=wip rows (the canonical WIP key); NULL for rm/pm/fg.';

-- WIP reads (get_sfg_on_hand / get_available_batches / mrp / the sfg-inventory
-- picker) filter on this; partial index keeps it cheap.
CREATE INDEX IF NOT EXISTS idx_inv_batch_sfg_code
    ON inventory_batch(sfg_code, status, entity) WHERE sfg_code IS NOT NULL;

-- Backfill existing WIP rows: they stored the code in sku_name, so copy it across.
-- Idempotent (only fills NULLs).
UPDATE inventory_batch
   SET sfg_code = sku_name
 WHERE item_type = 'wip' AND sfg_code IS NULL;

-- ---------------------------------------------------------------------------
-- GAP B: catalogue attributes that need the (absent) FINAL source.
-- ---------------------------------------------------------------------------
ALTER TABLE sfg_attributes ADD COLUMN IF NOT EXISTS va_article TEXT;
ALTER TABLE sfg_attributes ADD COLUMN IF NOT EXISTS primary_bu TEXT;

-- Re-define sfg_master to surface them (append-only columns, so the dependent
-- sfg_where_used view stays valid — same rule as 056).
CREATE OR REPLACE VIEW sfg_master AS
SELECT
    s.sku_id,
    s.sfg_code,
    s.particulars                          AS sfg_name,
    s.item_group,
    s.sub_group,
    'kg'::text                             AS uom,
    s.sale_group,
    s.gst,
    s.created_at,
    (SELECT MIN(COALESCE(r.practical_operation, r.process_name))
       FROM bom_process_route r
      WHERE r.output_code = s.sfg_code)     AS produced_at_stage,
    (SELECT COUNT(DISTINCT r2.bom_id)
       FROM bom_process_route r2
      WHERE r2.input_code = s.sfg_code)     AS consumed_by_fg_count,
    a.base_recipe,
    a.create_wip_operation,
    a.sfg_origin,
    a.va_article,
    a.primary_bu
FROM all_sku s
LEFT JOIN sfg_attributes a ON a.sfg_code = s.sfg_code
WHERE s.item_type = 'sfg'
  AND s.sfg_code IS NOT NULL;

COMMIT;
