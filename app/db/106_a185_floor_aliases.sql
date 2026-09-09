-- A185 floor mapping, decided by the warehouse team on 2026-09-09.
--
-- TWO DIFFERENT KINDS OF CHANGE IN ONE FILE
--
-- 1. ALIASES onto floors the ERP already declared:
--      PRODUCTION            -> Dmart Production Area   135 rows,  36,224.40 kg
--      PRODUCTION - ROASTING -> Roasting Area            43 rows,   4,756.29 kg
--    The second is the only A185 mapping the data itself supports: 73% of its
--    articles also appear on Roasting Area, and SATISHINGOLE is the only person
--    who counts either. The first is the team's call -- PRODUCTION is 81% RM,
--    which does not read like a Dmart-specific floor, but they know the building.
--
-- 2. AREAS THE PROFILE WAS MISSING, now added to FLOORS_BY_WAREHOUSE
--    (web_replica/src/lib/admin-api.ts) and populated here:
--      STORE, DOCK AREA      -> A185 Stores             144 rows,  86,176.88 kg
--      RACK, RACK AREA       -> A185 Stores Rack        519 rows, 350,829.97 kg
--    A185 never had a spelling problem the way W202 did -- 16 of its 18 undeclared
--    names had exactly one spelling each. It had a profile that omitted most of
--    the warehouse. RACK and RACK AREA are merged on the team's instruction; the
--    data agrees (same counter, 63% shared articles, both ~90% raw material).
--
--    A185 Cold is declared alongside them but is NOT populated here: the
--    instruction was to leave COLD, COLD 1 and COLD 2 as they are. Those three
--    still hold 190,257.12 kg on undeclared names.
--
-- DELIBERATELY UNTOUCHED: PACKING (left, as instructed), DOCK, the two
-- PACKING…MEZZANINE spellings and DOCK AND CHEESE AREA (each names two floors at
-- once, so no alias can be right), REJECTION COLD & RACK (116 of 116 rows are
-- stock_type 'Off Grade/Rejection' -- a stock type typed into the floor box, not
-- a location), and the test floors.

BEGIN;

-- Same reason as the W202 correction: the BEFORE UPDATE trigger would stamp
-- updated_at = now() on every row, making a data correction look like an edit to
-- any later reconcile against stocktake_entries. DDL is transactional, so the
-- trigger comes back even if this aborts.
ALTER TABLE new_stock_entries DISABLE TRIGGER trg_nse_touch;

UPDATE new_stock_entries SET floor_name = v.canon
  FROM (VALUES
      ('PRODUCTION',            'Dmart Production Area'),
      ('PRODUCTION - ROASTING', 'Roasting Area'),
      ('STORE',                 'A185 Stores'),
      ('DOCK AREA',             'A185 Stores'),
      ('RACK',                  'A185 Stores Rack'),
      ('RACK AREA',             'A185 Stores Rack')
  ) AS v(raw, canon)
 WHERE UPPER(BTRIM(new_stock_entries.warehouse)) = 'A185'
   AND UPPER(BTRIM(new_stock_entries.floor_name)) = v.raw;

ALTER TABLE new_stock_entries ENABLE TRIGGER trg_nse_touch;

COMMIT;
