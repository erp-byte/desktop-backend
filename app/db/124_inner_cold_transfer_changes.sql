-- ===========================================================================
-- 124_inner_cold_transfer_changes.sql
-- Inner Cold transfer: the record its delete needs to undo it.
--
-- POST /api/v1/transfer/inner-transfer is the one transfer path that edits
-- stock rows, by design: inside one cold store it relabels the lot of
-- {cfpl,cdpl}_cold_stocks rows in place, may correct their storage_location,
-- and splits a row without a box id when only part of it moves (the row keeps
-- the rest, a new row holds the cartons moved). Deleting the challan
-- (DELETE /api/v1/transfer/inner-transfer/{challan_no}) now UNDOES it, in one
-- transaction: every row it changed gets its old lot / location / cartons /
-- weight / value back and every row it inserted is deleted. The delete is
-- refused when one of those rows has been changed again since.
--
-- inner_cold_transfer_changes holds one row per cold-stock row a line of a
-- challan changed ('update') or inserted ('insert'): which table and row, the
-- tracked values before (old_*; none for an insert) and after (new_*). The
-- delete checks each row still holds its last change's new_* values, then
-- restores its first change's old_* values (or deletes an inserted row).
-- line_id is the inner_cold_transfer line the change belongs to; a line with
-- no change rows was saved before this table existed, and its challan cannot
-- be undone (the delete refuses it).
--
-- inner_cold_transfer itself was created at runtime by IMS (cold_storage_server
-- _ensure_inner_cold_transfer_table); it is created here too, with the same
-- columns, so the ERP does not depend on IMS for it. On a database that has it
-- already this does nothing but add the two location columns IMS adds when
-- they are missing.
--
-- MUST run BEFORE the backend deploys: the create refuses to save (503) until
-- inner_cold_transfer_changes exists. Idempotent; one transaction.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.inner_cold_transfer (
    id                    SERIAL PRIMARY KEY,
    challan_no            VARCHAR(50),
    transfer_date         VARCHAR(20),
    from_warehouse        VARCHAR(100),
    reason_code           VARCHAR(100),
    remark                TEXT,
    stock_record_id       INTEGER,
    item_category         VARCHAR(255),
    item_description      VARCHAR(255),
    net_weight_kg         NUMERIC(12, 3),
    quantity              INTEGER,
    old_lot_number        VARCHAR(100),
    new_lot_number        VARCHAR(100),
    new_storage_location  VARCHAR(255),
    old_storage_location  VARCHAR(255),
    transfer_type         VARCHAR(50) DEFAULT 'INNER_COLD',
    status                VARCHAR(20) DEFAULT 'COMPLETED',
    created_at            TIMESTAMP DEFAULT NOW()
);
ALTER TABLE public.inner_cold_transfer ADD COLUMN IF NOT EXISTS new_storage_location VARCHAR(255);
ALTER TABLE public.inner_cold_transfer ADD COLUMN IF NOT EXISTS old_storage_location VARCHAR(255);

CREATE TABLE IF NOT EXISTS public.inner_cold_transfer_changes (
    id                       BIGSERIAL PRIMARY KEY,
    challan_no               VARCHAR(50) NOT NULL,
    line_id                  INTEGER,
    cold_table               TEXT NOT NULL
                             CHECK (cold_table IN ('cfpl_cold_stocks', 'cdpl_cold_stocks')),
    row_id                   BIGINT NOT NULL,
    kind                     TEXT NOT NULL CHECK (kind IN ('update', 'insert')),
    old_lot_no               TEXT,
    old_storage_location     TEXT,
    old_no_of_cartons        NUMERIC,
    old_weight_kg            NUMERIC,
    old_total_inventory_kgs  NUMERIC,
    old_value                NUMERIC,
    new_lot_no               TEXT,
    new_storage_location     TEXT,
    new_no_of_cartons        NUMERIC,
    new_weight_kg            NUMERIC,
    new_total_inventory_kgs  NUMERIC,
    new_value                NUMERIC,
    created_at               TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_inner_cold_transfer_changes_challan
    ON public.inner_cold_transfer_changes (challan_no);
CREATE INDEX IF NOT EXISTS ix_inner_cold_transfer_changes_row
    ON public.inner_cold_transfer_changes (cold_table, row_id);

COMMIT;
