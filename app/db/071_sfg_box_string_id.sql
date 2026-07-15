-- 071_sfg_box_string_id.sql
-- sfg_box.carton_id (PK) and parent_box_id (self-ref): BIGINT -> TEXT.
--
-- New box ids are minted in the service as "<8-digit-time-base>-<per-JC counter>"
-- (e.g. "48213307-1", "48213307-2"), matching the po_box / RM box format. The
-- counter CONTINUES from the last box created for a job card, so a second create
-- call appends -N+1, -N+2, ... rather than resetting.
--
-- Existing integer rows backfill in place to their BARE decimal string
-- (e.g. 48213307 -> '48213307'), i.e. NO '-counter' suffix. The service's
-- per-JC counter SQL (split_part(carton_id,'-',2) ~ '^[0-9]+$') returns '' for a
-- bare id and excludes it, so the first NEW box on a legacy JC starts at
-- counter 1 with a fresh time-base and cannot collide with the bare legacy id.
--
-- There are NO foreign keys referencing sfg_box (parent_box_id is a bare
-- self-reference column, never a declared FK). The ONLY dependent object is the
-- sfg_genealogy view, which Postgres requires be dropped before ALTER COLUMN TYPE.
BEGIN;

-- (1) Drop the only object that blocks the in-place type change.
DROP VIEW IF EXISTS sfg_genealogy;

-- (2) Convert PK + self-ref in place. `USING ::text` IS the backfill. The PK
--     index (sfg_box_pkey) and idx_sfg_box_parent rebuild automatically. Guard
--     on data_type so a re-run / already-text DB is a no-op.
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
          WHERE table_name = 'sfg_box' AND column_name = 'carton_id') = 'bigint' THEN
        ALTER TABLE sfg_box ALTER COLUMN carton_id     TYPE TEXT USING carton_id::text;
    END IF;
    IF (SELECT data_type FROM information_schema.columns
          WHERE table_name = 'sfg_box' AND column_name = 'parent_box_id') = 'bigint' THEN
        ALTER TABLE sfg_box ALTER COLUMN parent_box_id TYPE TEXT USING parent_box_id::text;
    END IF;
END $$;

-- (3) Recreate the genealogy view. 067 dropped sfg_box.lot_number, so the body
--     must NOT reference it; carton_id is natively TEXT after the alter.
CREATE OR REPLACE VIEW sfg_genealogy AS
SELECT
    b.carton_id,
    b.sfg_code,
    b.parent_box_id,
    b.status,
    b.net_weight,
    b.job_card_id                AS producer_job_card_id,
    b.received_into_job_card_id,
    b.source_inventory_batch_id,
    ib.sku_name                  AS source_batch_sku_name,
    ib.lot_number                AS source_batch_lot,
    ib.entity                    AS source_batch_entity,
    ib.inward_date               AS source_batch_inward_date
FROM sfg_box b
LEFT JOIN inventory_batch ib ON ib.batch_id = b.source_inventory_batch_id;

COMMENT ON VIEW sfg_genealogy IS
    'SFG/FG box genealogy: box -> parent_box_id (box->box) -> source WIP inventory_batch/lot. carton_id and parent_box_id are TEXT (070).';

COMMIT;
