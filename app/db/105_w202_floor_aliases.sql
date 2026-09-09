-- Three more W202 floor aliases, supplied by the warehouse team on 2026-09-09.
--
--   BARLINE             -> Second Floor         806 rows,  80,054.13 kg
--   TOP FLOOR           -> Terrace              195 rows,  11,271.99 kg
--   CHOCOLATE 3RD FLOOR -> Second Floor Mezz     76 rows,  13,491.38 kg
--
-- These are NOT spelling variants. The 2026-09-08 backfill deliberately left
-- them alone because nothing in the data says where they belong -- "BARLINE" is
-- not a misspelling of "Second Floor". They are mapped here on the authority of
-- the people who work the floors, which is the only thing that could settle it.
--
-- NOTE: this is what finally puts stock on Second Floor Mezz, which the backfill
-- left empty. That was correct then and is superseded now.
--
-- The same three rows are added to the alias maps in
-- 103_new_stock_entries_backfill.sql and
-- 104_new_stock_entries_delta_sync.sql, so a fresh backfill or the next
-- delta produces the same result as this correction. All three maps must agree.

BEGIN;

-- The BEFORE UPDATE trigger sets updated_at = now() unconditionally. This is a
-- data correction, not a business edit: the column mirrors the floor app's own
-- updated_at, and letting it move would make every corrected row look changed to
-- any later reconcile that compares the two tables. DDL is transactional, so the
-- trigger is restored even if this aborts.
ALTER TABLE new_stock_entries DISABLE TRIGGER trg_nse_touch;

UPDATE new_stock_entries SET floor_name = v.canon
  FROM (VALUES
      ('BARLINE',             'Second Floor'),
      ('TOP FLOOR',           'Terrace'),
      ('CHOCOLATE 3RD FLOOR', 'Second Floor Mezz')
  ) AS v(raw, canon)
 WHERE UPPER(BTRIM(new_stock_entries.warehouse)) = 'W202'
   AND UPPER(BTRIM(new_stock_entries.floor_name)) = v.raw;

ALTER TABLE new_stock_entries ENABLE TRIGGER trg_nse_touch;

COMMIT;
