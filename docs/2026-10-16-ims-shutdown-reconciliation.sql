-- Final read-only reconciliation for the IMS close (2026-10-19) and for Scan & send
-- sending ERP Material In boxes. Run each block on its own (psql / DBeaver / pgAdmin),
-- 00 first and 99 last. 00 opens a READ ONLY, REPEATABLE READ transaction: the server
-- refuses any write and every block sees one snapshot. If a block fails (a table or
-- column P1 / P2 shows missing), the transaction is aborted: run 99, then 00 again,
-- and skip that block. Blocks are separated by lines starting with "-- @@".
-- The site lists copy backend app/core/sites.json as of 2026-10-16 14:40 IST.

-- @@ 00 read-only snapshot
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY;
SET LOCAL statement_timeout = '15min';
SET LOCAL lock_timeout = '5s';

-- @@ P1 tables present
SELECT t.tbl, to_regclass('public.' || t.tbl) IS NOT NULL AS table_exists
  FROM (VALUES ('po_header'), ('po_line'), ('po_section'), ('po_box'), ('inventory_batch'), ('all_sku'),
               ('qc_inward_inspection'), ('jc_box_scan'), ('floor_requisition_box'), ('issue_note_line'),
               ('cfpl_transactions_v2'), ('cdpl_transactions_v2'), ('cfpl_boxes_v2'), ('cdpl_boxes_v2'),
               ('cfpl_bulk_entry_boxes'), ('cdpl_bulk_entry_boxes'), ('cfpl_cold_stocks'), ('cdpl_cold_stocks'),
               ('interunit_transfers_header'), ('interunit_transfer_boxes'), ('interunit_transfer_in_header'),
               ('interunit_transfer_in_boxes'), ('pending_transfer_stock'), ('cold_transfer_in_headers'),
               ('cold_transfer_inboxes'), ('cfpl_rtv_header'), ('cdpl_rtv_header'), ('cfpl_rtv_lines'),
               ('cdpl_rtv_lines'), ('cfpl_rtv_boxes'), ('cdpl_rtv_boxes')) AS t(tbl)
 ORDER BY 1;

-- @@ P2 columns and widths
SELECT c.table_name, c.column_name, c.data_type, c.character_maximum_length, c.is_nullable
  FROM information_schema.columns c
 WHERE c.table_schema = 'public'
   AND (c.table_name, c.column_name) IN (VALUES
        ('po_header', 'deleted_at'), ('po_header', 'warehouse'), ('po_header', 'entity'), ('po_header', 'po_number'),
        ('po_header', 'invoice_number'), ('po_line', 'sku_name'), ('po_line', 'particulars'),
        ('po_line', 'match_source'), ('po_line', 'match_score'), ('po_box', 'box_id'), ('po_box', 'lot_number'),
        ('all_sku', 'particulars'), ('all_sku', 'item_type'),
        ('jc_box_scan', 'box_id'), ('jc_box_scan', 'transaction_no'),
        ('floor_requisition_box', 'box_code'), ('floor_requisition_box', 'box_table'),
        ('issue_note_line', 'box_id'),
        ('qc_inward_inspection', 'transaction_no'), ('qc_inward_inspection', 'sku_name'),
        ('qc_inward_inspection', 'sku_name_raw'), ('qc_inward_inspection', 'verdict'),
        ('qc_inward_inspection', 'cancelled_at'),
        ('cfpl_transactions_v2', 'po_number'), ('cfpl_transactions_v2', 'invoice_number'),
        ('cfpl_transactions_v2', 'vendor_supplier_name'), ('cfpl_transactions_v2', 'entry_date'),
        ('cdpl_transactions_v2', 'po_number'), ('cdpl_transactions_v2', 'invoice_number'),
        ('pending_transfer_stock', 'box_id'), ('pending_transfer_stock', 'transaction_no'),
        ('pending_transfer_stock', 'lot_no'), ('pending_transfer_stock', 'item_description'),
        ('pending_transfer_stock', 'article'), ('pending_transfer_stock', 'source_table'),
        ('pending_transfer_stock', 'source_row_id'), ('pending_transfer_stock', 'from_company'),
        ('interunit_transfer_boxes', 'box_id'), ('interunit_transfer_boxes', 'transaction_no'),
        ('interunit_transfer_boxes', 'article'), ('interunit_transfer_boxes', 'lot_number'),
        ('interunit_transfer_boxes', 'box_number'), ('interunit_transfer_boxes', 'created_at'),
        ('interunit_transfer_in_boxes', 'box_id'), ('interunit_transfer_in_boxes', 'transaction_no'),
        ('interunit_transfer_in_boxes', 'original_box_id'), ('interunit_transfer_in_boxes', 'transfer_out_box_id'),
        ('interunit_transfer_in_boxes', 'lot_number'), ('interunit_transfer_in_header', 'condition_remarks'),
        ('interunit_transfers_header', 'created_ts'), ('cold_transfer_inboxes', 'transaction_no'),
        ('cold_transfer_in_headers', 'transfer_out_id'), ('cfpl_cold_stocks', 'auto_created_from_inward'),
        ('cfpl_cold_stocks', 'inward_transaction_no'), ('cdpl_cold_stocks', 'auto_created_from_inward'),
        ('cdpl_cold_stocks', 'inward_transaction_no'), ('cfpl_rtv_header', 'factory_unit'),
        ('cdpl_rtv_header', 'factory_unit'), ('cfpl_rtv_boxes', 'box_id'), ('cdpl_rtv_boxes', 'box_id'))
 ORDER BY 1, 2;

-- @@ P2b the other Material In columns the new Scan & send code reads
-- Expect 14 rows. A missing one would fail Material In lookups, and at a cold From site
-- also dispatches that fall through to Material In (it is read before the box-ID-only
-- cold search), IMS boxes without an exact cold row included.
SELECT c.table_name, c.column_name, c.data_type
  FROM information_schema.columns c
 WHERE c.table_schema = 'public'
   AND (c.table_name, c.column_name) IN (VALUES
        ('po_box', 'line_number'), ('po_box', 'section_number'), ('po_box', 'box_number'),
        ('po_box', 'net_weight'), ('po_box', 'gross_weight'), ('po_section', 'lot_number'),
        ('po_line', 'item_category'), ('po_line', 'sub_category'), ('po_line', 'item_type'),
        ('po_line', 'uom'), ('all_sku', 'item_group'), ('all_sku', 'sub_group'),
        ('all_sku', 'uom'), ('all_sku', 'sku_id'))
 ORDER BY 1, 2;

-- @@ P3 unique indexes the transfer rules rely on
SELECT i.tablename, i.indexname, i.indexdef
  FROM pg_indexes i
 WHERE i.schemaname = 'public'
   AND i.indexdef ILIKE 'CREATE UNIQUE%'
   AND i.tablename IN ('pending_transfer_stock', 'interunit_transfer_in_boxes', 'interunit_transfer_boxes',
                       'po_box', 'cfpl_cold_stocks', 'cdpl_cold_stocks')
 ORDER BY 1, 2;

-- @@ MI-1 Material In in use: POs and boxes by company, inward site and number format
WITH site_map(skey, site, kind, company) AS (
    VALUES ('W202', 'W202', 'warehouse', NULL), ('WAREHOUSEW202', 'W202', 'warehouse', NULL),
           ('A185', 'A185', 'warehouse', NULL), ('WAREHOUSEA185', 'A185', 'warehouse', NULL),
           ('A185COLD', 'A185', 'warehouse', NULL), ('A101', 'A101', 'warehouse', NULL),
           ('WAREHOUSEA101', 'A101', 'warehouse', NULL), ('A68', 'A68', 'warehouse', NULL),
           ('WAREHOUSEA68', 'A68', 'warehouse', NULL), ('F53', 'F53', 'warehouse', NULL),
           ('WAREHOUSEF53', 'F53', 'warehouse', NULL), ('APMCF53', 'F53', 'warehouse', NULL),
           ('RISHI', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGE', 'Rishi', 'cold', 'cdpl'),
           ('RISHICOLD', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGEPVTLTD', 'Rishi', 'cold', 'cdpl'),
           ('SAVLAD39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLDSTORAGE', 'Savla D-39', 'cold', 'cfpl'),
           ('D39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLD', 'Savla D-39', 'cold', 'cfpl'),
           ('OLDSAVLA', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD34', 'Savla D-39', 'cold', 'cfpl'),
           ('SAVLAD514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLDSTORAGE', 'Savla D-514', 'cold', 'cfpl'),
           ('D514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLD', 'Savla D-514', 'cold', 'cfpl'),
           ('NEWSAVLA', 'Savla D-514', 'cold', 'cfpl'), ('SAVLABOND', 'Savla Bond', 'cold', 'cfpl'),
           ('SUPREME', 'Supreme', 'cold', 'cdpl'), ('SUPREMECOLDSTORAGE', 'Supreme', 'cold', 'cdpl'),
           ('SUPREMECOLD', 'Supreme', 'cold', 'cdpl'), ('ESKIMO', 'Eskimo', 'cold', 'cdpl'),
           ('ESKIMOCOLDSTORAGE', 'Eskimo', 'cold', 'cdpl'), ('ESKIMOCOLD', 'Eskimo', 'cold', 'cdpl'),
           ('COLDSTORAGE', 'Cold Storage', 'cold', NULL), ('DEVINT', 'Dev Int', 'unknown', NULL)
), bx AS (
    SELECT b.transaction_no, count(*) AS boxes,
           count(*) FILTER (WHERE b.net_weight IS NULL OR b.net_weight <= 0) AS boxes_without_net,
           count(*) FILTER (WHERE b.box_id !~ '^[0-9]{8}-[0-9]+$') AS odd_box_ids,
           min(b.created_at) AS first_box_at, max(b.created_at) AS last_box_at
      FROM po_box b
     GROUP BY b.transaction_no
)
SELECT h.entity,
       CASE WHEN h.deleted_at IS NULL THEN 'live' ELSE 'deleted' END AS po_state,
       coalesce(sm.site, CASE WHEN nullif(btrim(coalesce(h.warehouse, '')), '') IS NULL THEN '(no warehouse)'
                              ELSE 'UNKNOWN: ' || btrim(h.warehouse) END) AS inward_site,
       coalesce(sm.kind, 'unknown') AS site_kind,
       CASE WHEN h.transaction_no ~ '^TR-[0-9]{14}-[0-9]{3}$' THEN 'TR-...-NNN (current upload)'
            WHEN h.transaction_no ~ '^TR-[0-9]{14}$' THEN 'TR-... (legacy upload: the IMS shape)'
            ELSE 'other' END AS txn_format,
       count(*) AS pos, sum(bx.boxes) AS boxes, sum(bx.boxes_without_net) AS boxes_without_net,
       sum(bx.odd_box_ids) AS odd_box_ids, min(bx.first_box_at) AS first_box_at, max(bx.last_box_at) AS last_box_at
  FROM po_header h
  JOIN bx ON bx.transaction_no = h.transaction_no
  LEFT JOIN site_map sm ON sm.skey = upper(regexp_replace(coalesce(h.warehouse, ''), '[^A-Za-z0-9]', '', 'g'))
 GROUP BY 1, 2, 3, 4, 5
 ORDER BY 1, 2, 3, 5;

-- @@ MI-2a article names of Material In boxes (sizes the article decision)
WITH sku AS (
    SELECT DISTINCT ON (s.k) s.k, s.particulars
      FROM (SELECT upper(regexp_replace(btrim(a.particulars), '\s+', ' ', 'g')) AS k, a.particulars, a.sku_id,
                   lower(coalesce(a.item_type, '')) IN ('rm', 'pm', 'fg') AS usual
              FROM all_sku a
             WHERE lower(coalesce(a.item_type, '')) <> 'sfg') s
     ORDER BY s.k, s.usual DESC, s.sku_id
), ln AS (
    SELECT h.entity, l.transaction_no, l.line_number, l.sku_name, l.particulars, l.match_source, l.match_score,
           count(*) AS boxes
      FROM po_line l
      JOIN po_header h ON h.transaction_no = l.transaction_no AND h.deleted_at IS NULL
      JOIN po_box b ON b.transaction_no = l.transaction_no AND b.line_number = l.line_number
     GROUP BY 1, 2, 3, 4, 5, 6, 7
), x AS (
    SELECT ln.*, e.particulars AS exact_name,
           CASE WHEN e.k IS NOT NULL THEN '1 exact: the Tally name is an all_sku name'
                WHEN nullif(btrim(coalesce(ln.particulars, '')), '') IS NULL
                     THEN '3 none: no all_sku name, the box carries its Tally name'
                WHEN coalesce(ln.match_score, 0) < 0.70
                     THEN '2c fuzzy match scored below 0.70 (the matcher before 2026-09-04): not used'
                WHEN ARRAY(SELECT m[1] FROM regexp_matches(coalesce(ln.sku_name, ''), '([0-9]+(?:\.[0-9]+)?)', 'g') AS m ORDER BY 1)
                   = ARRAY(SELECT m[1] FROM regexp_matches(ln.particulars, '([0-9]+(?:\.[0-9]+)?)', 'g') AS m ORDER BY 1)
                     THEN '2a fuzzy match only, same numbers'
                ELSE '2b fuzzy match only, the numbers differ (likely another grade)' END AS basis
      FROM ln
      LEFT JOIN sku e ON e.k = upper(regexp_replace(btrim(coalesce(ln.sku_name, '')), '\s+', ' ', 'g'))
)
SELECT x.basis, x.entity, count(*) AS po_lines, sum(x.boxes) AS boxes, count(DISTINCT x.sku_name) AS tally_names,
       count(*) FILTER (WHERE x.exact_name IS NOT NULL AND x.particulars IS NULL) AS exact_but_matcher_found_none,
       count(*) FILTER (WHERE x.exact_name IS NOT NULL AND x.particulars IS NOT NULL
                          AND upper(regexp_replace(btrim(x.particulars), '\s+', ' ', 'g'))
                           <> upper(regexp_replace(btrim(x.exact_name), '\s+', ' ', 'g'))) AS exact_but_matcher_picked_another
  FROM x
 GROUP BY 1, 2
 ORDER BY 1, 2;

-- @@ MI-2b Material In lines without an exact all_sku name (review list)
WITH sku AS (
    SELECT DISTINCT upper(regexp_replace(btrim(a.particulars), '\s+', ' ', 'g')) AS k
      FROM all_sku a
     WHERE lower(coalesce(a.item_type, '')) <> 'sfg'
), ln AS (
    SELECT h.entity, l.transaction_no, l.line_number, l.sku_name, l.particulars, l.match_source, l.match_score,
           count(*) AS boxes
      FROM po_line l
      JOIN po_header h ON h.transaction_no = l.transaction_no AND h.deleted_at IS NULL
      JOIN po_box b ON b.transaction_no = l.transaction_no AND b.line_number = l.line_number
     GROUP BY 1, 2, 3, 4, 5, 6, 7
)
SELECT ln.entity, ln.sku_name AS tally_name, ln.particulars AS matched_all_sku_name,
       CASE WHEN nullif(btrim(coalesce(ln.particulars, '')), '') IS NULL THEN 'none'
            WHEN coalesce(ln.match_score, 0) < 0.70 THEN 'SCORE BELOW 0.70'
            WHEN ARRAY(SELECT m[1] FROM regexp_matches(coalesce(ln.sku_name, ''), '([0-9]+(?:\.[0-9]+)?)', 'g') AS m ORDER BY 1)
               = ARRAY(SELECT m[1] FROM regexp_matches(ln.particulars, '([0-9]+(?:\.[0-9]+)?)', 'g') AS m ORDER BY 1)
                 THEN 'same numbers' ELSE 'NUMBERS DIFFER' END AS check_numbers,
       max(ln.match_score) AS match_score, count(*) AS po_lines, sum(ln.boxes) AS boxes,
       min(ln.transaction_no) AS example_transaction
  FROM ln
 WHERE NOT EXISTS (SELECT 1 FROM sku
                    WHERE sku.k = upper(regexp_replace(btrim(coalesce(ln.sku_name, '')), '\s+', ' ', 'g')))
 GROUP BY 1, 2, 3, 4
 ORDER BY 4 DESC, 7 DESC, 2;

-- @@ MI-3 Box numbers that repeat inside one PO (manual "Box # + Txn" entry)
WITH live AS (
    SELECT b.transaction_no, b.box_number
      FROM po_box b
      JOIN po_header h ON h.transaction_no = b.transaction_no AND h.deleted_at IS NULL
), rep AS (
    SELECT transaction_no, box_number, count(*) AS n FROM live GROUP BY 1, 2
)
SELECT count(*) AS live_boxes,
       count(*) FILTER (WHERE rep.n > 1) AS boxes_whose_box_no_repeats_in_their_po,
       count(DISTINCT live.transaction_no) AS pos,
       count(DISTINCT live.transaction_no) FILTER (WHERE rep.n > 1) AS pos_with_repeated_box_nos
  FROM live
  JOIN rep ON rep.transaction_no = live.transaction_no AND rep.box_number = live.box_number;

-- @@ MI-4 Material In box ids that also exist in IMS stock tables or in the transfer history
WITH p AS (
    SELECT b.box_id, b.transaction_no FROM po_box b
), other AS (
    SELECT 'cfpl_boxes_v2'::text AS tbl, btrim(x.box_id) AS box_id, btrim(coalesce(x.transaction_no, '')) AS tno
      FROM cfpl_boxes_v2 x WHERE btrim(coalesce(x.box_id, '')) <> ''
    UNION ALL
    SELECT 'cdpl_boxes_v2', btrim(x.box_id), btrim(coalesce(x.transaction_no, ''))
      FROM cdpl_boxes_v2 x WHERE btrim(coalesce(x.box_id, '')) <> ''
    UNION ALL
    SELECT 'cfpl_bulk_entry_boxes', btrim(x.box_id), btrim(coalesce(x.transaction_no, ''))
      FROM cfpl_bulk_entry_boxes x WHERE btrim(coalesce(x.box_id, '')) <> ''
    UNION ALL
    SELECT 'cdpl_bulk_entry_boxes', btrim(x.box_id), btrim(coalesce(x.transaction_no, ''))
      FROM cdpl_bulk_entry_boxes x WHERE btrim(coalesce(x.box_id, '')) <> ''
    UNION ALL
    SELECT 'cfpl_cold_stocks', btrim(x.box_id), btrim(coalesce(x.transaction_no, ''))
      FROM cfpl_cold_stocks x WHERE btrim(coalesce(x.box_id, '')) <> ''
    UNION ALL
    SELECT 'cdpl_cold_stocks', btrim(x.box_id), btrim(coalesce(x.transaction_no, ''))
      FROM cdpl_cold_stocks x WHERE btrim(coalesce(x.box_id, '')) <> ''
    UNION ALL
    SELECT 'interunit_transfer_boxes (transfer history)', btrim(x.box_id), btrim(coalesce(x.transaction_no, ''))
      FROM interunit_transfer_boxes x WHERE btrim(coalesce(x.box_id, '')) <> ''
)
SELECT other.tbl,
       count(DISTINCT p.box_id) AS material_in_box_ids_also_here,
       count(DISTINCT p.box_id) FILTER (WHERE other.tno = p.transaction_no) AS same_box_id_and_transaction
  FROM p
  JOIN other ON other.box_id = p.box_id
 GROUP BY 1
 ORDER BY 1;

-- @@ MI-5 Material In values against the widths of the transfer columns they are copied into
WITH eff AS (
    SELECT b.box_id, b.transaction_no,
           coalesce(nullif(btrim(coalesce(b.lot_number, '')), ''), nullif(btrim(coalesce(s.lot_number, '')), '')) AS lot,
           l.sku_name, l.particulars
      FROM po_box b
      JOIN po_header h ON h.transaction_no = b.transaction_no AND h.deleted_at IS NULL
      LEFT JOIN po_section s ON s.transaction_no = b.transaction_no AND s.line_number = b.line_number
                            AND s.section_number = b.section_number
      LEFT JOIN po_line l ON l.transaction_no = b.transaction_no AND l.line_number = b.line_number
), lim AS (
    SELECT c.table_name || '.' || c.column_name AS col, c.character_maximum_length AS max_len
      FROM information_schema.columns c
     WHERE c.table_schema = 'public'
       AND (c.table_name, c.column_name) IN (VALUES
            ('pending_transfer_stock', 'lot_no'), ('pending_transfer_stock', 'item_description'),
            ('pending_transfer_stock', 'article'), ('pending_transfer_stock', 'box_id'),
            ('pending_transfer_stock', 'transaction_no'), ('interunit_transfer_boxes', 'lot_number'),
            ('interunit_transfer_boxes', 'article'), ('interunit_transfer_boxes', 'box_id'),
            ('interunit_transfer_in_boxes', 'box_id'), ('interunit_transfer_in_boxes', 'lot_number'),
            ('interunit_transfer_in_boxes', 'article'))
)
SELECT lim.col AS copied_into, v.what, lim.max_len AS column_limit,
       max(length(v.val)) AS longest_material_in_value,
       count(*) FILTER (WHERE lim.max_len IS NOT NULL AND length(v.val) > lim.max_len) AS values_over_the_limit
  FROM eff
 CROSS JOIN LATERAL (VALUES
       ('lot (box, else section)', eff.lot,
        ARRAY['pending_transfer_stock.lot_no', 'interunit_transfer_boxes.lot_number', 'interunit_transfer_in_boxes.lot_number']),
       ('Tally name (po_line.sku_name)', eff.sku_name,
        ARRAY['pending_transfer_stock.item_description', 'pending_transfer_stock.article',
              'interunit_transfer_boxes.article', 'interunit_transfer_in_boxes.article']),
       ('matched all_sku name (po_line.particulars)', eff.particulars,
        ARRAY['pending_transfer_stock.item_description', 'pending_transfer_stock.article',
              'interunit_transfer_boxes.article', 'interunit_transfer_in_boxes.article']),
       ('box_id', eff.box_id,
        ARRAY['pending_transfer_stock.box_id', 'interunit_transfer_boxes.box_id', 'interunit_transfer_in_boxes.box_id']),
       ('transaction_no', eff.transaction_no, ARRAY['pending_transfer_stock.transaction_no'])) AS v(what, val, cols)
  JOIN lim ON lim.col = ANY (v.cols)
 GROUP BY 1, 2, 3
 ORDER BY 1, 2;

-- @@ MI-6 Material In data drift: lots, weights, the inventory_batch copy
SELECT count(*) AS live_boxes,
       count(*) FILTER (WHERE b.net_weight IS NULL OR b.net_weight <= 0) AS no_net_weight,
       count(*) FILTER (WHERE nullif(btrim(coalesce(b.lot_number, '')), '') IS NULL
                          AND nullif(btrim(coalesce(s.lot_number, '')), '') IS NULL) AS no_lot_at_all,
       count(*) FILTER (WHERE nullif(btrim(coalesce(b.lot_number, '')), '') IS NOT NULL
                          AND nullif(btrim(coalesce(s.lot_number, '')), '') IS NOT NULL
                          AND btrim(b.lot_number) <> btrim(s.lot_number)) AS box_lot_differs_from_section_lot,
       count(*) FILTER (WHERE ib.batch_id IS NULL) AS no_inventory_batch,
       count(*) FILTER (WHERE ib.batch_id IS NOT NULL AND ib.original_qty_kg IS DISTINCT FROM b.net_weight) AS batch_weight_differs,
       count(*) FILTER (WHERE ib.batch_id IS NOT NULL AND ib.current_qty_kg < ib.original_qty_kg) AS batch_partly_issued,
       count(*) FILTER (WHERE ib.batch_id IS NOT NULL
                          AND btrim(coalesce(ib.lot_number, '')) <> btrim(coalesce(b.lot_number, ''))) AS batch_lot_differs,
       count(*) FILTER (WHERE ib.batch_id IS NOT NULL
                          AND upper(regexp_replace(coalesce(ib.warehouse_id, ''), '[^A-Za-z0-9]', '', 'g'))
                           <> upper(regexp_replace(coalesce(h.warehouse, ''), '[^A-Za-z0-9]', '', 'g'))) AS batch_warehouse_differs,
       count(*) FILTER (WHERE ib.status IS NOT NULL AND ib.status <> 'AVAILABLE') AS batch_not_available
  FROM po_box b
  JOIN po_header h ON h.transaction_no = b.transaction_no AND h.deleted_at IS NULL
  LEFT JOIN po_section s ON s.transaction_no = b.transaction_no AND s.line_number = b.line_number
                        AND s.section_number = b.section_number
  LEFT JOIN inventory_batch ib ON ib.batch_id = b.box_id;

-- @@ MI-7 the same goods inwarded in both IMS and ERP Material In
WITH erp AS (
    SELECT h.entity, h.transaction_no, h.po_number, h.invoice_number, h.vendor_supplier_name, h.warehouse,
           count(b.box_id) AS erp_boxes, min(b.created_at) AS first_erp_box_at
      FROM po_header h
      JOIN po_box b ON b.transaction_no = h.transaction_no
     WHERE h.deleted_at IS NULL
     GROUP BY 1, 2, 3, 4, 5, 6
), ims AS (
    SELECT 'cfpl'::text AS entity, t.transaction_no::text AS transaction_no, t.po_number::text AS po_number,
           t.invoice_number::text AS invoice_number, t.vendor_supplier_name::text AS vendor,
           t.warehouse::text AS warehouse, t.entry_date::text AS entry_date
      FROM cfpl_transactions_v2 t
    UNION ALL
    SELECT 'cdpl', t.transaction_no::text, t.po_number::text, t.invoice_number::text, t.vendor_supplier_name::text,
           t.warehouse::text, t.entry_date::text
      FROM cdpl_transactions_v2 t
)
SELECT erp.entity, erp.transaction_no AS erp_transaction, erp.po_number, erp.vendor_supplier_name,
       erp.invoice_number, erp.warehouse AS erp_warehouse, erp.erp_boxes, erp.first_erp_box_at,
       ims.transaction_no AS ims_transaction, ims.entry_date AS ims_entry_date, ims.warehouse AS ims_warehouse,
       CASE WHEN nullif(btrim(coalesce(ims.po_number, '')), '') IS NOT NULL
                 AND upper(btrim(ims.po_number)) = upper(btrim(coalesce(erp.po_number, '')))
            THEN 'same PO number' ELSE 'same invoice and vendor' END AS matched_on,
       (SELECT count(*) FROM cfpl_boxes_v2 x WHERE ims.entity = 'cfpl' AND x.transaction_no = ims.transaction_no)
     + (SELECT count(*) FROM cdpl_boxes_v2 x WHERE ims.entity = 'cdpl' AND x.transaction_no = ims.transaction_no)
           AS ims_boxes
  FROM erp
  JOIN ims ON ims.entity = erp.entity
          AND ((nullif(btrim(coalesce(ims.po_number, '')), '') IS NOT NULL
                AND upper(btrim(ims.po_number)) = upper(btrim(coalesce(erp.po_number, ''))))
            OR (nullif(btrim(coalesce(ims.invoice_number, '')), '') IS NOT NULL
                AND upper(btrim(ims.invoice_number)) = upper(btrim(coalesce(erp.invoice_number, '')))
                AND upper(regexp_replace(btrim(coalesce(ims.vendor, '')), '\s+', ' ', 'g'))
                  = upper(regexp_replace(btrim(coalesce(erp.vendor_supplier_name, '')), '\s+', ' ', 'g'))))
 ORDER BY erp.first_erp_box_at, erp.transaction_no;

-- @@ MI-8a Material In boxes scanned into a job card (skip if P1 shows no jc_box_scan)
SELECT count(DISTINCT b.box_id) AS boxes_scanned_into_a_job_card,
       count(DISTINCT j.job_card_id) AS job_cards
  FROM po_box b
  JOIN po_header h ON h.transaction_no = b.transaction_no AND h.deleted_at IS NULL
  JOIN jc_box_scan j ON j.box_id = b.box_id AND (j.transaction_no IS NULL OR j.transaction_no = b.transaction_no);

-- @@ MI-8b Material In boxes sent to production, issued, or judged by QC
WITH p AS (
    SELECT b.box_id, b.transaction_no, l.sku_name, l.particulars
      FROM po_box b
      JOIN po_header h ON h.transaction_no = b.transaction_no AND h.deleted_at IS NULL
      LEFT JOIN po_line l ON l.transaction_no = b.transaction_no AND l.line_number = b.line_number
)
SELECT 'sent to production on a floor requisition' AS used_by, count(DISTINCT p.box_id) AS boxes
  FROM p JOIN floor_requisition_box f ON f.box_code = p.box_id AND f.box_table = 'po_box'
UNION ALL
SELECT 'on an issue note line', count(DISTINCT p.box_id)
  FROM p JOIN issue_note_line i ON i.box_id = p.box_id
UNION ALL
SELECT 'inventory_batch status ' || ib.status, count(*)
  FROM p JOIN inventory_batch ib ON ib.batch_id = p.box_id
 GROUP BY ib.status
UNION ALL
SELECT 'QC: ' || CASE WHEN q.inspection_id IS NULL THEN 'no inspection found'
                      ELSE coalesce(q.verdict, 'no verdict yet (' || coalesce(q.status, '?') || ')') END, count(*)
  FROM p
  LEFT JOIN LATERAL (
       SELECT i.inspection_id, i.verdict, i.status
         FROM qc_inward_inspection i
        WHERE i.transaction_no = p.transaction_no
          AND i.cancelled_at IS NULL
          AND (upper(btrim(coalesce(i.sku_name, ''))) IN (upper(btrim(coalesce(p.sku_name, ''))), upper(btrim(coalesce(p.particulars, ''))))
               OR upper(btrim(coalesce(i.sku_name_raw, ''))) = upper(btrim(coalesce(p.sku_name, ''))))
        ORDER BY i.verdict_at DESC NULLS LAST, i.inspection_id DESC
        LIMIT 1) q ON true
 GROUP BY 1
 ORDER BY 1;

-- @@ R3-a where the ERP guard places every transferred box now (summary)
WITH site_map(skey, site, kind, company) AS (
    VALUES ('W202', 'W202', 'warehouse', NULL), ('WAREHOUSEW202', 'W202', 'warehouse', NULL),
           ('A185', 'A185', 'warehouse', NULL), ('WAREHOUSEA185', 'A185', 'warehouse', NULL),
           ('A185COLD', 'A185', 'warehouse', NULL), ('A101', 'A101', 'warehouse', NULL),
           ('WAREHOUSEA101', 'A101', 'warehouse', NULL), ('A68', 'A68', 'warehouse', NULL),
           ('WAREHOUSEA68', 'A68', 'warehouse', NULL), ('F53', 'F53', 'warehouse', NULL),
           ('WAREHOUSEF53', 'F53', 'warehouse', NULL), ('APMCF53', 'F53', 'warehouse', NULL),
           ('RISHI', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGE', 'Rishi', 'cold', 'cdpl'),
           ('RISHICOLD', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGEPVTLTD', 'Rishi', 'cold', 'cdpl'),
           ('SAVLAD39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLDSTORAGE', 'Savla D-39', 'cold', 'cfpl'),
           ('D39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLD', 'Savla D-39', 'cold', 'cfpl'),
           ('OLDSAVLA', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD34', 'Savla D-39', 'cold', 'cfpl'),
           ('SAVLAD514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLDSTORAGE', 'Savla D-514', 'cold', 'cfpl'),
           ('D514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLD', 'Savla D-514', 'cold', 'cfpl'),
           ('NEWSAVLA', 'Savla D-514', 'cold', 'cfpl'), ('SAVLABOND', 'Savla Bond', 'cold', 'cfpl'),
           ('SUPREME', 'Supreme', 'cold', 'cdpl'), ('SUPREMECOLDSTORAGE', 'Supreme', 'cold', 'cdpl'),
           ('SUPREMECOLD', 'Supreme', 'cold', 'cdpl'), ('ESKIMO', 'Eskimo', 'cold', 'cdpl'),
           ('ESKIMOCOLDSTORAGE', 'Eskimo', 'cold', 'cdpl'), ('ESKIMOCOLD', 'Eskimo', 'cold', 'cdpl'),
           ('COLDSTORAGE', 'Cold Storage', 'cold', NULL), ('DEVINT', 'Dev Int', 'unknown', NULL)
), tb AS (
    SELECT b.id AS box_row_id, btrim(b.box_id) AS box_id, btrim(b.transaction_no) AS tno,
           h.id AS transfer_id, h.status, h.to_site, h.created_ts,
           greatest(h.created_ts, b.created_at) AS joined_ts,
           mod(mod((extract(epoch FROM h.created_ts) * 1000)::bigint - h.id::bigint, 100000000)
               + 100000000, 100000000) AS d
      FROM interunit_transfer_boxes b
      JOIN interunit_transfers_header h ON h.id = b.header_id
     WHERE btrim(coalesce(b.box_id, '')) <> ''
       AND btrim(coalesce(b.transaction_no, '')) NOT IN ('', 'DIRECT')
), latest AS (
    SELECT DISTINCT ON (tb.box_id, tb.tno) tb.*
      FROM tb
     ORDER BY tb.box_id, tb.tno, (tb.joined_ts IS NOT NULL) DESC, tb.joined_ts DESC NULLS LAST, tb.transfer_id DESC
), grn AS (
    SELECT ih.id, ih.transfer_out_id, lower(btrim(coalesce(ih.status, ''))) AS st
      FROM interunit_transfer_in_header ih
), cgrn AS (
    SELECT ch.id, ch.transfer_out_id, lower(btrim(coalesce(ch.status, ''))) AS st
      FROM cold_transfer_in_headers ch
), v AS (
    SELECT l.*, lower(btrim(coalesce(l.status, ''))) AS st,
           CASE WHEN l.created_ts IS NULL THEN 'unknown'
                WHEN least(l.d, 100000000 - l.d) <= 3000 OR abs(l.d - 19800000) <= 3000 THEN 'ERP'
                ELSE 'IMS' END AS dispatched_by,
           EXISTS (SELECT 1 FROM grn g WHERE g.transfer_out_id = l.transfer_id) AS has_grn,
           EXISTS (SELECT 1 FROM cgrn c WHERE c.transfer_out_id = l.transfer_id) AS has_cold_grn,
           (EXISTS (SELECT 1 FROM grn g WHERE g.transfer_out_id = l.transfer_id AND g.st = 'received')
            OR EXISTS (SELECT 1 FROM cgrn c WHERE c.transfer_out_id = l.transfer_id AND c.st = 'received')) AS has_received_receipt,
           EXISTS (SELECT 1 FROM cold_transfer_inboxes cb JOIN cgrn c ON c.id = cb.header_id
                    WHERE c.transfer_out_id = l.transfer_id
                      AND btrim(cb.box_id) = l.box_id AND btrim(cb.transaction_no) = l.tno) AS taken_by_cold_grn,
           EXISTS (SELECT 1 FROM interunit_transfer_in_boxes ib JOIN grn g ON g.id = ib.header_id
                    WHERE g.transfer_out_id = l.transfer_id
                      AND ((ib.transfer_out_box_id IS NOT NULL AND ib.transfer_out_box_id = l.box_row_id)
                           OR (l.box_id IN (btrim(coalesce(ib.box_id, '')), btrim(coalesce(ib.original_box_id, '')))
                               AND (btrim(coalesce(ib.transaction_no, '')) = ''
                                    OR btrim(ib.transaction_no) = l.tno)))) AS acked
      FROM latest l
), w AS (
    SELECT v.*,
           (v.st = 'received' OR v.has_received_receipt) AS received,
           CASE WHEN v.taken_by_cold_grn THEN 'arrived'
                WHEN v.st = 'received' OR v.has_received_receipt
                     THEN CASE WHEN NOT (v.has_grn OR v.has_cold_grn) THEN 'in_transit'
                               WHEN v.acked THEN 'arrived' ELSE 'short' END
                WHEN v.st = 'partially received' AND v.acked THEN 'arrived'
                ELSE 'in_transit' END AS erp_verdict
      FROM v
)
SELECT w.erp_verdict,
       CASE WHEN w.received THEN 'received'
            WHEN w.st = 'partially received' THEN 'partially received'
            WHEN w.st IN ('dispatch', 'partial', 'in transit', 'completed') THEN 'in transit'
            ELSE 'other status: ' || w.st END AS latest_transfer_state,
       CASE WHEN w.erp_verdict <> 'arrived' THEN '-'
            ELSE coalesce(sm.site, 'UNKNOWN: ' || coalesce(btrim(w.to_site), '')) END AS sendable_only_from,
       w.dispatched_by, count(*) AS boxes
  FROM w
  LEFT JOIN site_map sm ON sm.skey = upper(regexp_replace(coalesce(w.to_site, ''), '[^A-Za-z0-9]', '', 'g'))
 GROUP BY 1, 2, 3, 4
 ORDER BY 1, 2, 3, 4;

-- @@ R3-b received boxes the ERP guard refuses from every site (detail)
WITH site_map(skey, site, kind, company) AS (
    VALUES ('W202', 'W202', 'warehouse', NULL), ('WAREHOUSEW202', 'W202', 'warehouse', NULL),
           ('A185', 'A185', 'warehouse', NULL), ('WAREHOUSEA185', 'A185', 'warehouse', NULL),
           ('A185COLD', 'A185', 'warehouse', NULL), ('A101', 'A101', 'warehouse', NULL),
           ('WAREHOUSEA101', 'A101', 'warehouse', NULL), ('A68', 'A68', 'warehouse', NULL),
           ('WAREHOUSEA68', 'A68', 'warehouse', NULL), ('F53', 'F53', 'warehouse', NULL),
           ('WAREHOUSEF53', 'F53', 'warehouse', NULL), ('APMCF53', 'F53', 'warehouse', NULL),
           ('RISHI', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGE', 'Rishi', 'cold', 'cdpl'),
           ('RISHICOLD', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGEPVTLTD', 'Rishi', 'cold', 'cdpl'),
           ('SAVLAD39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLDSTORAGE', 'Savla D-39', 'cold', 'cfpl'),
           ('D39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLD', 'Savla D-39', 'cold', 'cfpl'),
           ('OLDSAVLA', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD34', 'Savla D-39', 'cold', 'cfpl'),
           ('SAVLAD514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLDSTORAGE', 'Savla D-514', 'cold', 'cfpl'),
           ('D514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLD', 'Savla D-514', 'cold', 'cfpl'),
           ('NEWSAVLA', 'Savla D-514', 'cold', 'cfpl'), ('SAVLABOND', 'Savla Bond', 'cold', 'cfpl'),
           ('SUPREME', 'Supreme', 'cold', 'cdpl'), ('SUPREMECOLDSTORAGE', 'Supreme', 'cold', 'cdpl'),
           ('SUPREMECOLD', 'Supreme', 'cold', 'cdpl'), ('ESKIMO', 'Eskimo', 'cold', 'cdpl'),
           ('ESKIMOCOLDSTORAGE', 'Eskimo', 'cold', 'cdpl'), ('ESKIMOCOLD', 'Eskimo', 'cold', 'cdpl'),
           ('COLDSTORAGE', 'Cold Storage', 'cold', NULL), ('DEVINT', 'Dev Int', 'unknown', NULL)
), tb AS (
    SELECT b.id AS box_row_id, btrim(b.box_id) AS box_id, btrim(b.transaction_no) AS tno, b.article, b.lot_number,
           h.id AS transfer_id, h.challan_no, h.status, h.from_site, h.to_site, h.created_ts,
           greatest(h.created_ts, b.created_at) AS joined_ts,
           mod(mod((extract(epoch FROM h.created_ts) * 1000)::bigint - h.id::bigint, 100000000)
               + 100000000, 100000000) AS d
      FROM interunit_transfer_boxes b
      JOIN interunit_transfers_header h ON h.id = b.header_id
     WHERE btrim(coalesce(b.box_id, '')) <> ''
       AND btrim(coalesce(b.transaction_no, '')) NOT IN ('', 'DIRECT')
), latest AS (
    SELECT DISTINCT ON (tb.box_id, tb.tno) tb.*
      FROM tb
     ORDER BY tb.box_id, tb.tno, (tb.joined_ts IS NOT NULL) DESC, tb.joined_ts DESC NULLS LAST, tb.transfer_id DESC
), grn AS (
    SELECT ih.id, ih.transfer_out_id, lower(btrim(coalesce(ih.status, ''))) AS st,
           coalesce(ih.condition_remarks, '') LIKE '%Closed with shortage%' AS closed_short
      FROM interunit_transfer_in_header ih
), cgrn AS (
    SELECT ch.id, ch.transfer_out_id, lower(btrim(coalesce(ch.status, ''))) AS st
      FROM cold_transfer_in_headers ch
), v AS (
    SELECT l.*, lower(btrim(coalesce(l.status, ''))) AS st,
           CASE WHEN l.created_ts IS NULL THEN 'unknown'
                WHEN least(l.d, 100000000 - l.d) <= 3000 OR abs(l.d - 19800000) <= 3000 THEN 'ERP'
                ELSE 'IMS' END AS dispatched_by,
           EXISTS (SELECT 1 FROM grn g WHERE g.transfer_out_id = l.transfer_id) AS has_grn,
           EXISTS (SELECT 1 FROM cgrn c WHERE c.transfer_out_id = l.transfer_id) AS has_cold_grn,
           (EXISTS (SELECT 1 FROM grn g WHERE g.transfer_out_id = l.transfer_id AND g.st = 'received')
            OR EXISTS (SELECT 1 FROM cgrn c WHERE c.transfer_out_id = l.transfer_id AND c.st = 'received')) AS has_received_receipt,
           EXISTS (SELECT 1 FROM grn g WHERE g.transfer_out_id = l.transfer_id AND g.closed_short) AS closed_short,
           EXISTS (SELECT 1 FROM cold_transfer_inboxes cb JOIN cgrn c ON c.id = cb.header_id
                    WHERE c.transfer_out_id = l.transfer_id
                      AND btrim(cb.box_id) = l.box_id AND btrim(cb.transaction_no) = l.tno) AS taken_by_cold_grn,
           EXISTS (SELECT 1 FROM interunit_transfer_in_boxes ib JOIN grn g ON g.id = ib.header_id
                    WHERE g.transfer_out_id = l.transfer_id
                      AND ((ib.transfer_out_box_id IS NOT NULL AND ib.transfer_out_box_id = l.box_row_id)
                           OR (l.box_id IN (btrim(coalesce(ib.box_id, '')), btrim(coalesce(ib.original_box_id, '')))
                               AND (btrim(coalesce(ib.transaction_no, '')) = ''
                                    OR btrim(ib.transaction_no) = l.tno)))) AS acked,
           (SELECT string_agg(DISTINCT btrim(ib.transaction_no), ', ')
              FROM interunit_transfer_in_boxes ib JOIN grn g ON g.id = ib.header_id
             WHERE g.transfer_out_id = l.transfer_id
               AND l.box_id IN (btrim(coalesce(ib.box_id, '')), btrim(coalesce(ib.original_box_id, '')))
               AND btrim(coalesce(ib.transaction_no, '')) NOT IN ('', l.tno)) AS acked_under_other_tno
      FROM latest l
), w AS (
    SELECT v.*,
           (v.st = 'received' OR v.has_received_receipt) AS received,
           CASE WHEN v.taken_by_cold_grn THEN 'arrived'
                WHEN v.st = 'received' OR v.has_received_receipt
                     THEN CASE WHEN NOT (v.has_grn OR v.has_cold_grn) THEN 'in_transit'
                               WHEN v.acked THEN 'arrived' ELSE 'short' END
                WHEN v.st = 'partially received' AND v.acked THEN 'arrived'
                ELSE 'in_transit' END AS erp_verdict
      FROM v
)
SELECT CASE WHEN w.erp_verdict = 'in_transit'
                 THEN 'R2 Received, but no receipt in either ledger: the ERP says in transit'
            WHEN w.erp_verdict = 'short' AND w.acked_under_other_tno IS NOT NULL
                 THEN 'R3 acknowledged under transaction ' || w.acked_under_other_tno || ': the ERP says short'
            WHEN w.erp_verdict = 'short' AND w.closed_short
                 THEN 'R6 written off by close-with-shortage: the ERP says short'
            WHEN w.erp_verdict = 'short'
                 THEN 'R7 not acknowledged on its receipt: the ERP says short'
            ELSE 'R8 received at a site the site table does not know: refused from every From site'
       END AS reason,
       w.dispatched_by, w.box_id, w.tno, w.article, w.lot_number, w.challan_no, w.from_site, w.to_site,
       w.status, w.created_ts
  FROM w
  LEFT JOIN site_map sm ON sm.skey = upper(regexp_replace(coalesce(w.to_site, ''), '[^A-Za-z0-9]', '', 'g'))
 WHERE (w.received AND w.erp_verdict IN ('in_transit', 'short'))
    OR (w.erp_verdict = 'arrived' AND sm.site IS NULL)
 ORDER BY 1, w.created_ts, w.challan_no, w.box_id;

-- @@ X-1 challans dispatched by one app and received (or still to be received) by the other
WITH site_map(skey, site, kind, company) AS (
    VALUES ('W202', 'W202', 'warehouse', NULL), ('WAREHOUSEW202', 'W202', 'warehouse', NULL),
           ('A185', 'A185', 'warehouse', NULL), ('WAREHOUSEA185', 'A185', 'warehouse', NULL),
           ('A185COLD', 'A185', 'warehouse', NULL), ('A101', 'A101', 'warehouse', NULL),
           ('WAREHOUSEA101', 'A101', 'warehouse', NULL), ('A68', 'A68', 'warehouse', NULL),
           ('WAREHOUSEA68', 'A68', 'warehouse', NULL), ('F53', 'F53', 'warehouse', NULL),
           ('WAREHOUSEF53', 'F53', 'warehouse', NULL), ('APMCF53', 'F53', 'warehouse', NULL),
           ('RISHI', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGE', 'Rishi', 'cold', 'cdpl'),
           ('RISHICOLD', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGEPVTLTD', 'Rishi', 'cold', 'cdpl'),
           ('SAVLAD39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLDSTORAGE', 'Savla D-39', 'cold', 'cfpl'),
           ('D39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLD', 'Savla D-39', 'cold', 'cfpl'),
           ('OLDSAVLA', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD34', 'Savla D-39', 'cold', 'cfpl'),
           ('SAVLAD514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLDSTORAGE', 'Savla D-514', 'cold', 'cfpl'),
           ('D514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLD', 'Savla D-514', 'cold', 'cfpl'),
           ('NEWSAVLA', 'Savla D-514', 'cold', 'cfpl'), ('SAVLABOND', 'Savla Bond', 'cold', 'cfpl'),
           ('SUPREME', 'Supreme', 'cold', 'cdpl'), ('SUPREMECOLDSTORAGE', 'Supreme', 'cold', 'cdpl'),
           ('SUPREMECOLD', 'Supreme', 'cold', 'cdpl'), ('ESKIMO', 'Eskimo', 'cold', 'cdpl'),
           ('ESKIMOCOLDSTORAGE', 'Eskimo', 'cold', 'cdpl'), ('ESKIMOCOLD', 'Eskimo', 'cold', 'cdpl'),
           ('COLDSTORAGE', 'Cold Storage', 'cold', NULL), ('DEVINT', 'Dev Int', 'unknown', NULL)
), h AS (
    SELECT h.*,
           mod(mod((extract(epoch FROM h.created_ts) * 1000)::bigint - h.id::bigint, 100000000)
               + 100000000, 100000000) AS d
      FROM interunit_transfers_header h
), hc AS (
    SELECT h.id, h.challan_no, h.stock_trf_date, h.created_ts, h.created_by, h.from_site, h.to_site, h.status,
           CASE WHEN h.created_ts IS NULL THEN 'unknown'
                WHEN least(h.d, 100000000 - h.d) <= 3000 OR abs(h.d - 19800000) <= 3000 THEN 'ERP'
                ELSE 'IMS' END AS dispatched_by,
           coalesce(sf.kind, 'unknown') AS from_kind, coalesce(st.kind, 'unknown') AS to_kind
      FROM h
      LEFT JOIN site_map sf ON sf.skey = upper(regexp_replace(coalesce(h.from_site, ''), '[^A-Za-z0-9]', '', 'g'))
      LEFT JOIN site_map st ON st.skey = upper(regexp_replace(coalesce(h.to_site, ''), '[^A-Za-z0-9]', '', 'g'))
), gi0 AS (
    SELECT ih.*,
           mod(mod((extract(epoch FROM ih.created_at) * 1000)::bigint - ih.id::bigint, 100000000)
               + 100000000, 100000000) AS d
      FROM interunit_transfer_in_header ih
), gi1 AS (
    SELECT gi0.*,
           coalesce(least(gi0.d, 100000000 - gi0.d) <= 10000 OR abs(gi0.d - 19800000) <= 10000, false) AS erp
      FROM gi0
), gi AS (
    SELECT gi1.transfer_out_id, count(*) AS grns,
           bool_or(gi1.erp) AS erp_grn, bool_or(NOT gi1.erp) AS ims_grn,
           bool_or(NOT gi1.erp AND lower(coalesce(gi1.status, '')) = 'pending') AS ims_grn_open,
           bool_or(lower(coalesce(gi1.status, '')) = 'received') AS grn_received,
           bool_or(gi1.inward_transaction_no IS NOT NULL) AS ims_relabelled,
           string_agg(coalesce(gi1.grn_number, '#' || gi1.id) || ' ' || coalesce(gi1.status, '?')
                      || CASE WHEN gi1.erp THEN ' (ERP)' ELSE ' (IMS)' END, '; ' ORDER BY gi1.id) AS grns_list
      FROM gi1
     GROUP BY gi1.transfer_out_id
), cg AS (
    SELECT ch.transfer_out_id, count(DISTINCT ch.id) AS cold_grns,
           bool_or(lower(coalesce(ch.status, '')) <> 'received') AS cold_grn_open,
           bool_or(lower(coalesce(ch.status, '')) = 'received') AS cold_grn_received,
           count(cb.id) AS cold_inboxes,
           string_agg(DISTINCT coalesce(ch.grn_number, '#' || ch.id) || ' ' || coalesce(ch.status, '?'), '; ') AS cold_grns_list
      FROM cold_transfer_in_headers ch
      LEFT JOIN cold_transfer_inboxes cb ON cb.header_id = ch.id
     GROUP BY ch.transfer_out_id
), bx AS (
    SELECT b.header_id,
           count(*) FILTER (WHERE btrim(coalesce(b.box_id, '')) <> ''
                              AND btrim(coalesce(b.transaction_no, '')) NOT IN ('', 'DIRECT')) AS real_boxes
      FROM interunit_transfer_boxes b
     GROUP BY b.header_id
), pd AS (
    SELECT p.transfer_out_id,
           count(*) FILTER (WHERE p.status = 'In Transit' AND p.box_id NOT LIKE 'LINE-%') AS boxes_in_transit,
           count(*) FILTER (WHERE p.status = 'In Transit' AND p.box_id LIKE 'LINE-%') AS line_units_in_transit
      FROM pending_transfer_stock p
     GROUP BY p.transfer_out_id
), y AS (
    SELECT hc.*,
           (lower(btrim(coalesce(hc.status, ''))) = 'received' OR coalesce(gi.grn_received, false)
            OR coalesce(cg.cold_grn_received, false)) AS is_received,
           coalesce(bx.real_boxes, 0) AS real_boxes,
           coalesce(pd.boxes_in_transit, 0) AS boxes_in_transit,
           coalesce(pd.line_units_in_transit, 0) AS line_units_in_transit,
           coalesce(gi.grns, 0) AS grns, coalesce(gi.erp_grn, false) AS erp_grn,
           coalesce(gi.ims_grn, false) AS ims_grn, coalesce(gi.ims_grn_open, false) AS ims_grn_open,
           coalesce(gi.ims_relabelled, false) AS ims_relabelled, gi.grns_list,
           coalesce(cg.cold_grns, 0) AS cold_grns, coalesce(cg.cold_grn_open, false) AS cold_grn_open,
           coalesce(cg.cold_inboxes, 0) AS cold_inboxes, cg.cold_grns_list,
           CASE WHEN coalesce(gi.erp_grn, false) AND (coalesce(gi.ims_grn, false) OR coalesce(cg.cold_grns, 0) > 0)
                     THEN 'ERP+IMS'
                WHEN coalesce(gi.erp_grn, false) THEN 'ERP'
                WHEN coalesce(gi.ims_grn, false) OR coalesce(cg.cold_grns, 0) > 0 THEN 'IMS'
                ELSE 'none' END AS received_by
      FROM hc
      LEFT JOIN bx ON bx.header_id = hc.id
      LEFT JOIN pd ON pd.transfer_out_id = hc.id
      LEFT JOIN gi ON gi.transfer_out_id = hc.id
      LEFT JOIN cg ON cg.transfer_out_id = hc.id
)
SELECT CASE
         WHEN y.cold_grn_open OR y.ims_grn_open THEN 'C IMS receipt started, not finished: finish it in IMS before 19-Oct'
         WHEN y.dispatched_by = 'ERP' AND y.received_by IN ('IMS', 'ERP+IMS') THEN 'A ERP dispatch, IMS receipt'
         WHEN y.dispatched_by = 'IMS' AND y.received_by IN ('ERP', 'ERP+IMS') THEN 'B IMS dispatch, ERP receipt'
         WHEN y.dispatched_by = 'IMS' AND NOT y.is_received THEN 'D IMS dispatch, not received yet'
         WHEN y.is_received AND y.grns = 0 AND y.cold_grns = 0 THEN 'E Received, no receipt in either app'
         WHEN y.is_received AND y.grns = 0 THEN 'F Received only through the IMS cold receipt'
       END AS category,
       y.id AS transfer_id, y.challan_no, y.stock_trf_date, y.created_ts, y.created_by,
       y.dispatched_by, y.received_by, y.from_site, y.from_kind, y.to_site, y.to_kind, y.status,
       y.real_boxes, y.boxes_in_transit, y.line_units_in_transit,
       y.grns_list, y.ims_relabelled, y.cold_grns_list, y.cold_inboxes
  FROM y
 WHERE y.cold_grn_open OR y.ims_grn_open
    OR (y.dispatched_by = 'IMS' AND NOT y.is_received)
    OR (y.dispatched_by = 'ERP' AND y.received_by IN ('IMS', 'ERP+IMS'))
    OR (y.dispatched_by = 'IMS' AND y.received_by IN ('ERP', 'ERP+IMS'))
    OR (y.is_received AND y.grns = 0)
 ORDER BY 1, y.created_ts;

-- @@ X-2 cold-unit customer returns whose boxes were left to IMS
WITH site_map(skey, site, kind, company) AS (
    VALUES ('W202', 'W202', 'warehouse', NULL), ('WAREHOUSEW202', 'W202', 'warehouse', NULL),
           ('A185', 'A185', 'warehouse', NULL), ('WAREHOUSEA185', 'A185', 'warehouse', NULL),
           ('A185COLD', 'A185', 'warehouse', NULL), ('A101', 'A101', 'warehouse', NULL),
           ('WAREHOUSEA101', 'A101', 'warehouse', NULL), ('A68', 'A68', 'warehouse', NULL),
           ('WAREHOUSEA68', 'A68', 'warehouse', NULL), ('F53', 'F53', 'warehouse', NULL),
           ('WAREHOUSEF53', 'F53', 'warehouse', NULL), ('APMCF53', 'F53', 'warehouse', NULL),
           ('RISHI', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGE', 'Rishi', 'cold', 'cdpl'),
           ('RISHICOLD', 'Rishi', 'cold', 'cdpl'), ('RISHICOLDSTORAGEPVTLTD', 'Rishi', 'cold', 'cdpl'),
           ('SAVLAD39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLDSTORAGE', 'Savla D-39', 'cold', 'cfpl'),
           ('D39', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD39COLD', 'Savla D-39', 'cold', 'cfpl'),
           ('OLDSAVLA', 'Savla D-39', 'cold', 'cfpl'), ('SAVLAD34', 'Savla D-39', 'cold', 'cfpl'),
           ('SAVLAD514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLDSTORAGE', 'Savla D-514', 'cold', 'cfpl'),
           ('D514', 'Savla D-514', 'cold', 'cfpl'), ('SAVLAD514COLD', 'Savla D-514', 'cold', 'cfpl'),
           ('NEWSAVLA', 'Savla D-514', 'cold', 'cfpl'), ('SAVLABOND', 'Savla Bond', 'cold', 'cfpl'),
           ('SUPREME', 'Supreme', 'cold', 'cdpl'), ('SUPREMECOLDSTORAGE', 'Supreme', 'cold', 'cdpl'),
           ('SUPREMECOLD', 'Supreme', 'cold', 'cdpl'), ('ESKIMO', 'Eskimo', 'cold', 'cdpl'),
           ('ESKIMOCOLDSTORAGE', 'Eskimo', 'cold', 'cdpl'), ('ESKIMOCOLD', 'Eskimo', 'cold', 'cdpl'),
           ('COLDSTORAGE', 'Cold Storage', 'cold', NULL), ('DEVINT', 'Dev Int', 'unknown', NULL)
), cr AS (
    SELECT 'CFPL'::text AS company, h.id AS header_id, h.rtv_id, h.rtv_date, h.factory_unit, h.customer,
           h.status, h.created_by, h.created_ts
      FROM cfpl_rtv_header h
    UNION ALL
    SELECT 'CDPL', h.id, h.rtv_id, h.rtv_date, h.factory_unit, h.customer, h.status, h.created_by, h.created_ts
      FROM cdpl_rtv_header h
), flags AS (
    SELECT cr.*, sm.site AS unit_site,
           coalesce(sm.kind = 'cold', false) AS erp_refuses_box_entry,
           lower(regexp_replace(coalesce(cr.factory_unit, ''), '[^A-Za-z0-9]', '', 'g'))
               IN ('savlad39', 'd39', 'savlabond', 'oldsavla', 'savlad39cold', 'savlad514', 'd514',
                   'newsavla', 'savlad514cold', 'rishi', 'rishicold', 'rishicoldstorage', 'supreme',
                   'supremecold', 'supremecoldstorage', 'eskimo', 'eskimocold', 'eskimocoldstorage') AS ims_mirrors_to_cold
      FROM cr
      LEFT JOIN site_map sm ON sm.skey = upper(regexp_replace(coalesce(cr.factory_unit, ''), '[^A-Za-z0-9]', '', 'g'))
), bx AS (
    SELECT 'CFPL'::text AS company, b.header_id, count(*) AS box_rows,
           array_agg(btrim(b.box_id)) FILTER (WHERE btrim(coalesce(b.box_id, '')) <> '') AS printed_ids
      FROM cfpl_rtv_boxes b GROUP BY b.header_id
    UNION ALL
    SELECT 'CDPL', b.header_id, count(*),
           array_agg(btrim(b.box_id)) FILTER (WHERE btrim(coalesce(b.box_id, '')) <> '')
      FROM cdpl_rtv_boxes b GROUP BY b.header_id
), ln AS (
    SELECT 'CFPL'::text AS company, l.header_id, count(*) AS lines, sum(l.qty) AS line_qty
      FROM cfpl_rtv_lines l GROUP BY l.header_id
    UNION ALL
    SELECT 'CDPL', l.header_id, count(*), sum(l.qty) FROM cdpl_rtv_lines l GROUP BY l.header_id
), mir AS (
    SELECT 'CFPL'::text AS company, c.inward_transaction_no AS rtv_id, count(*) AS cold_rows,
           array_agg(btrim(c.box_id)) FILTER (WHERE btrim(coalesce(c.box_id, '')) <> '') AS cold_ids
      FROM cfpl_cold_stocks c
     WHERE c.auto_created_from_inward IS TRUE AND c.inward_transaction_no IS NOT NULL
     GROUP BY c.inward_transaction_no
    UNION ALL
    SELECT 'CDPL', c.inward_transaction_no, count(*),
           array_agg(btrim(c.box_id)) FILTER (WHERE btrim(coalesce(c.box_id, '')) <> '')
      FROM cdpl_cold_stocks c
     WHERE c.auto_created_from_inward IS TRUE AND c.inward_transaction_no IS NOT NULL
     GROUP BY c.inward_transaction_no
), x AS (
    SELECT f.*, coalesce(ln.lines, 0) AS lines, ln.line_qty, coalesce(bx.box_rows, 0) AS box_rows,
           coalesce(cardinality(bx.printed_ids), 0) AS printed_boxes, coalesce(mir.cold_rows, 0) AS cold_rows,
           (SELECT count(*) FROM unnest(coalesce(bx.printed_ids, '{}'::text[])) AS i(box_id)
             WHERE NOT (i.box_id = ANY (coalesce(mir.cold_ids, '{}'::text[])))) AS printed_not_in_cold
      FROM flags f
      LEFT JOIN ln ON ln.company = f.company AND ln.header_id = f.header_id
      LEFT JOIN bx ON bx.company = f.company AND bx.header_id = f.header_id
      LEFT JOIN mir ON mir.company = f.company AND mir.rtv_id = f.rtv_id
     WHERE f.erp_refuses_box_entry OR f.ims_mirrors_to_cold
)
SELECT CASE WHEN lower(coalesce(x.status, '')) = 'rejected' THEN '9 rejected'
            WHEN x.box_rows = 0 THEN '1 NO BOXES ENTERED: enter them in IMS before 19-Oct'
            WHEN x.printed_boxes < x.box_rows
                 THEN '2 ' || (x.box_rows - x.printed_boxes) || ' box(es) entered but not printed: print them in IMS'
            WHEN NOT x.ims_mirrors_to_cold THEN '3 unit IMS never mirrors: printed boxes are in no cold stock'
            WHEN x.printed_not_in_cold > 0 THEN '4 printed boxes missing from cold stock: re-save the boxes in IMS'
            ELSE '8 complete' END AS state,
       x.company, x.rtv_id, x.rtv_date, x.factory_unit, x.unit_site, x.customer, x.status, x.created_by,
       x.created_ts, x.erp_refuses_box_entry, x.ims_mirrors_to_cold, x.lines, x.line_qty, x.box_rows,
       x.printed_boxes, x.cold_rows, x.printed_not_in_cold
  FROM x
 ORDER BY 1, x.created_ts;

-- @@ X-3 triggers on the stock, transfer and purchase tables (hidden writers that outlive IMS)
WITH tbl AS (
    SELECT c.oid, n.nspname AS schema_name, c.relname AS table_name
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
       AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
       AND (c.relname ~ '^(cfpl|cdpl)_' OR c.relname ~ '^(interunit_|cold_)'
            OR c.relname IN ('pending_transfer_stock', 'transfer_box_reconciliation', 'inner_cold_transfer',
                             'po_header', 'po_line', 'po_section', 'po_box', 'inventory_batch', 'warehouse_sites'))
)
SELECT t.schema_name, t.table_name, tg.tgname AS trigger_name,
       CASE tg.tgenabled WHEN 'O' THEN 'enabled' WHEN 'A' THEN 'enabled (always)'
                         WHEN 'R' THEN 'enabled (replica only)' WHEN 'D' THEN 'DISABLED' END AS state,
       pg_get_triggerdef(tg.oid, true) AS trigger_definition,
       tg.tgfoid::regprocedure AS trigger_function,
       pg_get_functiondef(tg.tgfoid) AS function_source
  FROM tbl t
  JOIN pg_trigger tg ON tg.tgrelid = t.oid AND NOT tg.tgisinternal
 ORDER BY t.table_name, tg.tgname;

-- @@ X-4 transferred boxes that are in no stock table, by dispatch day and app (a 404 on Scan & send)
WITH h AS (
    SELECT h.id, h.challan_no, h.created_ts, h.status, h.from_site, h.to_site,
           mod(mod((extract(epoch FROM h.created_ts) * 1000)::bigint - h.id::bigint, 100000000)
               + 100000000, 100000000) AS d
      FROM interunit_transfers_header h
), tb AS (
    SELECT btrim(b.box_id) AS box_id, btrim(b.transaction_no) AS tno, h.id AS transfer_id, h.created_ts,
           CASE WHEN h.created_ts IS NULL THEN 'unknown'
                WHEN least(h.d, 100000000 - h.d) <= 3000 OR abs(h.d - 19800000) <= 3000 THEN 'ERP'
                ELSE 'IMS' END AS dispatched_by
      FROM interunit_transfer_boxes b
      JOIN h ON h.id = b.header_id
     WHERE btrim(coalesce(b.box_id, '')) <> ''
       AND btrim(coalesce(b.transaction_no, '')) NOT IN ('', 'DIRECT')
), stockkeys AS (
    SELECT btrim(box_id) AS box_id, btrim(coalesce(transaction_no, '')) AS tno
      FROM cfpl_cold_stocks WHERE btrim(coalesce(box_id, '')) <> ''
    UNION SELECT btrim(box_id), btrim(coalesce(transaction_no, '')) FROM cdpl_cold_stocks WHERE btrim(coalesce(box_id, '')) <> ''
    UNION SELECT btrim(box_id), btrim(coalesce(transaction_no, '')) FROM cfpl_boxes_v2 WHERE btrim(coalesce(box_id, '')) <> ''
    UNION SELECT btrim(box_id), btrim(coalesce(transaction_no, '')) FROM cdpl_boxes_v2 WHERE btrim(coalesce(box_id, '')) <> ''
    UNION SELECT btrim(box_id), btrim(coalesce(transaction_no, '')) FROM cfpl_bulk_entry_boxes WHERE btrim(coalesce(box_id, '')) <> ''
    UNION SELECT btrim(box_id), btrim(coalesce(transaction_no, '')) FROM cdpl_bulk_entry_boxes WHERE btrim(coalesce(box_id, '')) <> ''
    UNION SELECT box_id, transaction_no FROM po_box
)
SELECT tb.created_ts::date AS dispatch_day, tb.dispatched_by,
       count(DISTINCT tb.transfer_id) AS transfers, count(*) AS boxes,
       count(*) FILTER (WHERE s.box_id IS NULL) AS boxes_in_no_stock_table,
       round(100.0 * count(*) FILTER (WHERE s.box_id IS NULL) / count(*), 1) AS pct_in_no_stock_table,
       max(tb.created_ts) FILTER (WHERE s.box_id IS NULL) AS last_dispatch_that_lost_a_row,
       min(tb.created_ts) FILTER (WHERE s.box_id IS NOT NULL) AS first_dispatch_that_kept_a_row
  FROM tb
  LEFT JOIN stockkeys s ON s.box_id = tb.box_id AND s.tno = tb.tno
 GROUP BY 1, 2
 ORDER BY 1, 2;

-- @@ 99 end
ROLLBACK;
