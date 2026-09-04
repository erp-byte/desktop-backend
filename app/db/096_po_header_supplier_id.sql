-- 096_po_header_supplier_id.sql — adds the po_header.supplier_id column that the
-- purchase module has always assumed exists.
--
-- WHY: po_header was created (po_schema.sql:3) without supplier_id, but the
-- purchase code references it in four places:
--   • po_commit._HEADER_COLUMNS + the INSERT/UPDATE column lists — every commit
--     in a batch aborts with
--         column "supplier_id" of relation "po_header" does not exist
--   • po_query._EQUALITY_FIELDS  — ?supplier_id=… list filter
--   • po_query._SORTABLE_COLUMNS — ?sort_by=supplier_id
--   • po_diff._HEADER_FIELDS     — duplicate-PO diffing
-- Nothing ever ALTERed it in, so the column is missing on every database that
-- was built from these files. tests/services/test_po_header_schema_drift.py
-- pins the code lists against app/db/*.sql so this cannot drift again.
--
-- TYPE: TEXT, matching the API contract — PreviewHeader.supplier_id and
-- PoListItem.supplier_id are `str | None` (schemas/po_api.py), and the list
-- filter arrives as `supplier_id: str | None = Query(None)` (po_router.py:179).
-- Deliberately NOT an FK: the Tally PO book carries no supplier key, so the
-- column is populated by hand (or left NULL) rather than resolved against a
-- supplier master. It is nullable with no default — every existing row keeps
-- reading as "no supplier recorded".
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS. scripts/migrate.py re-executes every
-- file on every deploy, so re-running is a no-op.
--
-- COST: instant on PG 11+ — nullable, no DEFAULT, so no table rewrite and no
-- ACCESS EXCLUSIVE lock beyond the catalog update.

ALTER TABLE po_header
    ADD COLUMN IF NOT EXISTS supplier_id TEXT;

-- Supports the ?supplier_id=… equality filter. Partial: supplier_id is NULL on
-- every pre-existing row and stays NULL for Tally-sourced POs, and there is no
-- point indexing the NULLs.
CREATE INDEX IF NOT EXISTS idx_po_header_supplier_id
    ON po_header(supplier_id)
 WHERE supplier_id IS NOT NULL;
