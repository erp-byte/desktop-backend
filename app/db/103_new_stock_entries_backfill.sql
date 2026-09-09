-- Copy stocktake_entries -> new_stock_entries, rewriting floor_name to the
-- canonical floor names the ERP declares in FLOORS_BY_WAREHOUSE
-- (web_replica/src/lib/admin-api.ts:367).
--
-- WHY THE REWRITE IS NEEDED
--   floor_name is free text. 3,495 of 8,590 rows (41%) carry a trailing space,
--   which alone splits every major floor in two: "LOWER BASEMENT " (592 rows)
--   and "LOWER BASEMENT" (92) are the same place. On top of that the same floor
--   is typed several ways -- First Floor appears as FIRST FLOOR, 1 ST FLOOR,
--   1ST FLOOR and ST FLOOR -- so three of W202's seven declared floors looked
--   like they had never been counted.
--
-- WHAT IS AND IS NOT REWRITTEN
--   Only names that resolve to a floor the ERP actually declares are rewritten.
--   A name with no declared counterpart (BARLINE, RACK, COLD, STORE, ...) keeps
--   its own text, trimmed. Those stay SHOUTING while canonical ones are mixed
--   case, so an uncanonicalised floor is visible at a glance instead of being
--   silently invented into a floor that does not exist.
--
--   Nothing is lost either way: id is preserved, so any row can be joined back
--   to stocktake_entries to recover the exact string that was typed.
--
-- TWO CONVERSIONS THAT ARE NOT COSMETIC
--   1. created_at/updated_at/verified_at are `timestamp WITHOUT time zone`
--      holding UTC in the source, and `timestamptz` in the target. They must be
--      told what they are -- `AT TIME ZONE 'UTC'` -- or every timestamp shifts
--      by the server offset and the IST business day moves with it.
--   2. new_stock_entries.id is GENERATED ALWAYS, so preserving the source id
--      requires OVERRIDING SYSTEM VALUE, and the identity sequence has to be
--      advanced past the copied ids afterwards or the next insert collides.

BEGIN;

-- Re-running would double every row: the PK would reject the second copy, but
-- fail halfway through. Refuse up front instead.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM new_stock_entries) THEN
        RAISE EXCEPTION
            'new_stock_entries already holds % row(s) - refusing to double-load',
            (SELECT COUNT(*) FROM new_stock_entries);
    END IF;
END $$;

CREATE TEMP TABLE floor_alias (
    warehouse text NOT NULL,
    raw       text NOT NULL,   -- UPPER(BTRIM(floor_name)) as typed
    canon     text NOT NULL,   -- exactly as FLOORS_BY_WAREHOUSE spells it
    PRIMARY KEY (warehouse, raw)
) ON COMMIT DROP;

INSERT INTO floor_alias (warehouse, raw, canon) VALUES
    -- ── W202: the 7 declared floors ──────────────────────────────────
    ('W202', 'LOWER BASEMENT',            'Lower Basement'),
    ('W202', 'UPPER BASEMENT',            'Upper Basement'),
    ('W202', 'TERRACE',                   'Terrace'),
    ('W202', 'FIRST FLOOR',               'First Floor'),
    ('W202', '1 ST FLOOR',                'First Floor'),
    ('W202', '1ST FLOOR',                 'First Floor'),
    ('W202', 'ST FLOOR',                  'First Floor'),   -- truncated "1ST FLOOR"
    ('W202', '1ST MEZZANINE',             'First Floor Mezz'),
    ('W202', '1ST - MAZZO',               'First Floor Mezz'),
    ('W202', '1ST FLOOR MAZZE',           'First Floor Mezz'),
    ('W202', '2ND FLOOR',                 'Second Floor'),
    -- Supplied by the warehouse team 2026-09-09; not spelling variants, so the
    -- data alone could never have produced them. See 105_w202_floor_aliases.sql.
    ('W202', 'BARLINE',                   'Second Floor'),
    ('W202', 'TOP FLOOR',                 'Terrace'),
    ('W202', 'CHOCOLATE 3RD FLOOR',       'Second Floor Mezz'),
    -- Reads as "the chocolate floor, which is the 2nd floor". This is the one
    -- merge carrying real weight on an inference rather than a spelling
    -- (256 rows, 29,879 kg); if chocolate is a separate area, drop this row.
    ('W202', 'CHOCOLATE FLOOR 2ND FLOOR', 'Second Floor'),

    -- ── A185: the 9 declared floors ──────────────────────────────────
    ('A185', 'ROASTING AREA',             'Roasting Area'),
    ('A185', 'MEZZANINE',                 'Mezzanine'),
    ('A185', 'MAZERNINE',                 'Mezzanine'),      -- misspelling
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

    -- ── Not a declared floor, but unambiguously one spelling of another.
    --    Kept upper case: merging a typo is not the same as canonicalising.
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
    -- No alias => keep what was typed, minus the stray whitespace.
    COALESCE(a.canon, NULLIF(BTRIM(s.floor_name), ''))::varchar(255),
    s.total_quantity, s.unit_uom, s.total_weight,
    s.entered_by, s.entered_by_email, s.authority,
    s.created_at AT TIME ZONE 'UTC',
    s.updated_at AT TIME ZONE 'UTC',
    s.stock_type, s.entry_id, s.status, s.is_checked, s.verified, s.verified_by,
    s.verified_at AT TIME ZONE 'UTC',
    s.remark, s.edits, s.source_kind, s.movement_id
FROM stocktake_entries s
LEFT JOIN floor_alias a
       ON a.warehouse = UPPER(BTRIM(COALESCE(s.warehouse,  '')))
      AND a.raw       = UPPER(BTRIM(COALESCE(s.floor_name, '')));

-- GENERATED ALWAYS keeps its own counter; without this the next insert reuses
-- id 1 and trips the primary key.
SELECT setval(pg_get_serial_sequence('new_stock_entries', 'id'),
              (SELECT MAX(id) FROM new_stock_entries));

COMMIT;
