-- Declare "Store" as a W202 floor and canonicalise the rows already on it.
--
-- STORE was the largest W202 floor name the ERP profile never declared: 301
-- rows, 51,790.59 kg, still being counted as recently as 2026-09-02. Until now
-- it could be read but not posted to, and no user could be granted it, because
-- a floor has to exist in FLOORS_BY_WAREHOUSE before the admin screen can offer
-- it. This adds it there (web_replica/src/lib/admin-api.ts and the server-side
-- mirror app/modules/stock_take/floors.py) and brings the spelling into line.
--
-- NOT the same place as A185's STORE, which 106 mapped to "A185 Stores" -- that
-- one is 81% raw material, this one belongs to W202. The alias is keyed on
-- (warehouse, raw) precisely so the same word can mean different floors in
-- different buildings.
--
-- The same row goes into the alias maps in 103 and 104, so a fresh backfill or
-- the next delta sync produces this result rather than re-introducing STORE.

BEGIN;

-- Same reason as 105/106: the BEFORE UPDATE trigger would stamp updated_at =
-- now() on every row, making a data correction look like an edit to any later
-- reconcile against stocktake_entries. DDL is transactional, so the trigger is
-- restored even if this aborts.
ALTER TABLE new_stock_entries DISABLE TRIGGER trg_nse_touch;

UPDATE new_stock_entries
   SET floor_name = 'Store'
 WHERE UPPER(BTRIM(warehouse))  = 'W202'
   AND UPPER(BTRIM(floor_name)) = 'STORE';

ALTER TABLE new_stock_entries ENABLE TRIGGER trg_nse_touch;

COMMIT;
