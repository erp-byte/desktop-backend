-- Copy rows the floor app has written to stocktake_entries since the backfill
-- into new_stock_entries, canonicalising floor_name on the way.
--
-- Re-runnable: it copies only ids new_stock_entries does not already hold, so
-- running it twice is a no-op and running it daily keeps the console current.
-- It is the stopgap, not the fix — the fix is for the floor app to write
-- canonical names into one table. See 103_new_stock_entries_backfill.sql.
--
-- DRAFTS ARE COPIED. The console filters them out itself
-- (latest_stock_service._build_filters), so copying them changes nothing it
-- shows; not copying them would mean a draft silently appearing out of nowhere
-- on the day it is submitted, with no row to trace it back to.
--
-- THE ID COLLISION THIS ALSO CLOSES
--   Both tables assign ids from their OWN sequence, and preserving source ids on
--   copy means new_stock_entries holds floor-app ids. Left alone, the console's
--   own adjustment rows would draw from the same low range the floor app is
--   still issuing, and the next sync would hit a primary-key collision on an id
--   that means two different things.
--   So the console's sequence is pushed to a range the floor app will not reach.
--   Copied rows keep their source id and still join back to stocktake_entries;
--   console-originated rows are recognisable by being above the watermark.

BEGIN;

CREATE TEMP TABLE floor_alias (
    warehouse text NOT NULL,
    raw       text NOT NULL,   -- UPPER(BTRIM(floor_name)) as typed
    canon     text NOT NULL,   -- exactly as FLOORS_BY_WAREHOUSE spells it
    PRIMARY KEY (warehouse, raw)
) ON COMMIT DROP;

-- Must stay identical to the backfill's map.
INSERT INTO floor_alias (warehouse, raw, canon) VALUES
    ('W202', 'LOWER BASEMENT',            'Lower Basement'),
    ('W202', 'UPPER BASEMENT',            'Upper Basement'),
    ('W202', 'TERRACE',                   'Terrace'),
    ('W202', 'FIRST FLOOR',               'First Floor'),
    ('W202', '1 ST FLOOR',                'First Floor'),
    ('W202', '1ST FLOOR',                 'First Floor'),
    ('W202', 'ST FLOOR',                  'First Floor'),
    ('W202', '1ST MEZZANINE',             'First Floor Mezz'),
    ('W202', '1ST - MAZZO',               'First Floor Mezz'),
    ('W202', '1ST FLOOR MAZZE',           'First Floor Mezz'),
    ('W202', '2ND FLOOR',                 'Second Floor'),
    -- Supplied by the warehouse team 2026-09-09; not spelling variants, so the
    -- data alone could never have produced them. See 105_w202_floor_aliases.sql.
    ('W202', 'BARLINE',                   'Second Floor'),
    ('W202', 'TOP FLOOR',                 'Terrace'),
    ('W202', 'CHOCOLATE 3RD FLOOR',       'Second Floor Mezz'),
    ('W202', 'CHOCOLATE FLOOR 2ND FLOOR', 'Second Floor'),
    ('A185', 'ROASTING AREA',             'Roasting Area'),
    ('A185', 'MEZZANINE',                 'Mezzanine'),
    ('A185', 'MAZERNINE',                 'Mezzanine'),
    ('A185', 'CHEESE FLOOR',              'Cheese Floor'),
    ('A185', 'PACKING FLOOR (DMART)',     'Dmart Packing Area'),
    -- Decided by the warehouse team 2026-09-09; see 106_a185_floor_aliases.sql.
    -- The last three areas were added to FLOORS_BY_WAREHOUSE at the same time.
    ('A185', 'PRODUCTION',                'Dmart Production Area'),
    ('A185', 'PRODUCTION - ROASTING',     'Roasting Area'),
    ('A185', 'STORE',                     'A185 Stores'),
    ('A185', 'DOCK AREA',                 'A185 Stores'),
    ('A185', 'RACK',                      'A185 Stores Rack'),
    ('A185', 'RACK AREA',                 'A185 Stores Rack'),
    ('A185', 'COLD2',                     'COLD 2');

INSERT INTO new_stock_entries (
    id, item_name, item_type, item_category, item_subcategory,
    warehouse, floor_name, total_quantity, unit_uom, total_weight,
    entered_by, entered_by_email, authority, created_at, updated_at,
    stock_type, entry_id, status, is_checked, verified, verified_by,
    verified_at, remark, edits, source_kind, movement_id
)
OVERRIDING SYSTEM VALUE
SELECT
    s.id,
    s.item_name, s.item_type, s.item_category, s.item_subcategory,
    BTRIM(s.warehouse),
    COALESCE(a.canon, NULLIF(BTRIM(s.floor_name), ''))::varchar(255),
    s.total_quantity, s.unit_uom, s.total_weight,
    s.entered_by, s.entered_by_email, s.authority,
    -- Naive-UTC source column -> timestamptz target. Without the explicit 'UTC'
    -- these land 5.5 hours out and the IST business day moves with them.
    s.created_at AT TIME ZONE 'UTC',
    s.updated_at AT TIME ZONE 'UTC',
    s.stock_type, s.entry_id, s.status, s.is_checked, s.verified, s.verified_by,
    s.verified_at AT TIME ZONE 'UTC',
    s.remark, s.edits, s.source_kind, s.movement_id
FROM stocktake_entries s
LEFT JOIN floor_alias a
       ON a.warehouse = UPPER(BTRIM(COALESCE(s.warehouse,  '')))
      AND a.raw       = UPPER(BTRIM(COALESCE(s.floor_name, '')))
WHERE NOT EXISTS (SELECT 1 FROM new_stock_entries n WHERE n.id = s.id);

-- Keep the console's own inserts clear of every id the floor app can still
-- issue. GREATEST so re-running never walks the sequence backwards.
SELECT setval('new_stock_entries_id_seq',
              GREATEST(10000000, (SELECT COALESCE(MAX(id), 0) FROM new_stock_entries) + 1));

COMMIT;
