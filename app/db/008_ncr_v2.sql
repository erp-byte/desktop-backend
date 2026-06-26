-- =========================================================================
-- 008_ncr_v2.sql
-- Pass-2 fixes for the NCR module:
--   • HI-02: add `ncr_record.entity` column for entity-scope filtering
--            (mirrors po_header.entity / coa_document.entity), backfill from
--            po_header where transaction_no anchors a PO.
--
-- Idempotent. Safe to re-run. PostgreSQL ≥ 11.
-- =========================================================================

ALTER TABLE ncr_record
    ADD COLUMN IF NOT EXISTS entity TEXT;

-- Backfill: derive entity from the po_header row when transaction_no resolves.
UPDATE ncr_record n
   SET entity = h.entity
  FROM po_header h
 WHERE n.transaction_no = h.transaction_no
   AND n.entity IS NULL
   AND h.entity IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ncr_entity ON ncr_record(entity);
