-- ===========================================================================
-- 132_po_receiving_status_backfill.sql
-- Recompute po_header.status for POs that already have boxes, by the rule the
-- backend now applies on every box change (services/receipt_match.py):
--
--   a line is matched when its net weight received reaches 99% of its ordered
--   weight (po_weight); the box count is checked only where the boxes record a
--   count, or where the line has no ordered weight (then the number of boxes);
--
--   pending            -- no box
--   extra received     -- more net weight than the ordered weight, in total
--   fully received     -- every line matched
--   partially received -- otherwise
--
-- Until now no PO was ever 'fully received': the status only knew pending /
-- partially / extra. Rows already right are left alone. Idempotent; data only.
-- ===========================================================================

BEGIN;

WITH per_line AS (
    SELECT l.transaction_no, l.line_number, l.po_weight, l.pack_count,
           COALESCE(SUM(b.net_weight), 0) AS rec_wt,
           SUM(b.count)                   AS counted,
           COUNT(b.box_id)                AS n_boxes
      FROM po_line l
      LEFT JOIN po_box b
             ON b.transaction_no = l.transaction_no AND b.line_number = l.line_number
     GROUP BY l.transaction_no, l.line_number, l.po_weight, l.pack_count
), judged AS (
    SELECT transaction_no, po_weight, rec_wt, n_boxes,
           (po_weight IS NULL OR rec_wt >= po_weight * 0.99)
       AND (NOT (pack_count IS NOT NULL AND (counted IS NOT NULL OR po_weight IS NULL))
            OR COALESCE(counted, n_boxes) >= pack_count) AS matched
      FROM per_line
), po AS (
    SELECT transaction_no,
           SUM(n_boxes)                AS n_boxes,
           SUM(rec_wt)                 AS rec_wt,
           COALESCE(SUM(po_weight), 0) AS ord_wt,
           bool_and(matched)           AS all_matched
      FROM judged
     GROUP BY transaction_no
), target AS (
    SELECT transaction_no,
           CASE WHEN n_boxes = 0                    THEN 'pending'
                WHEN ord_wt > 0 AND rec_wt > ord_wt THEN 'extra received'
                WHEN all_matched                    THEN 'fully received'
                ELSE 'partially received' END AS status
      FROM po
)
UPDATE po_header h
   SET status = t.status
  FROM target t
 WHERE h.transaction_no = t.transaction_no
   AND h.deleted_at IS NULL
   AND h.status IS DISTINCT FROM t.status;

COMMIT;
