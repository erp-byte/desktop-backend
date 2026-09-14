-- 109_stocktake_txn_code_widen.sql — a 4th sequence digit, because 999/day ran out.
--
-- WHAT HAPPENED
-- On 2026-09-13 the warehouse posted a full floor count through the console. The
-- day's codes ran 26256001 .. 26256999 and stopped: 982 rows committed and 17
-- numbers burned by transactions that took a code and then rolled back. At about
-- 22:00 IST gen_stocktake_txn_code() began raising
--
--     stocktake_transactions has reached 999 adjustments for 2026-09-13 …
--
-- on every further post. POST /transactions caught only ValueError, so the RAISE
-- reached FastAPI unhandled and the operator's phone showed nothing but
-- "Internal server error". They stopped for the night; the first thing posted
-- the next morning — 26257001, 08:25:07, TANDOORI SEASONING at A185 Cold — is
-- the item that had failed the evening before.
--
-- 99/day was raised to 999/day in 100_stocktake_txn_code_format.sql on the
-- reasoning that 99 "was too tight a ceiling to leave in a ledger that must
-- never refuse a real stock movement". One ordinary counting day then used 100%
-- of the replacement. 999 is the same mistake one order of magnitude up.
--
-- THE CHANGE
--     was   YYDDD + NNN    8 chars,   999/day
--     now   YYDDD + NNNN   9 chars,  9999/day
--
-- The date prefix, the timezone and the per-day reset are all unchanged; only
-- the width of the counter moves.
--
-- NOTHING IS RENUMBERED. 099 and 100 could renumber history because no code had
-- left the building; 1018 of them have now been quoted on paperwork and read
-- down phones, and a reference number that changes underneath the person holding
-- it is worse than a mixed-width format. Codes minted before this file stay 8
-- characters and stay valid; codes minted after it are 9. Both are unambiguous —
-- the first five characters are the day either way, and the rest is the counter —
-- which is why the generator reads the tail with SUBSTRING(txn_code FROM 6) and
-- no length, and why the shape CHECK now admits both widths.
--
-- WHY NOT KEEP 8 CHARACTERS
-- The only way to find a fourth counter digit inside eight is to give up a digit
-- of the year (YDDD + NNNN), which makes codes collide every ten years and would
-- force exactly the renumbering of history this file refuses. Nine characters is
-- the cheaper price.
--
-- THE 17 BURNED NUMBERS ARE NOT ADDRESSED HERE. A transaction that mints a code
-- and then fails leaves a permanent hole in that day's sequence, because the
-- next caller reads MAX()+1 over committed rows only. At 999/day that waste
-- mattered; at 9999/day it does not, and closing it properly means not minting
-- the code until the row is otherwise known to be good — a bigger change than
-- this incident justifies.
--
-- Idempotent: the whole file re-executes on every scripts/migrate.py run.

BEGIN;

-- ── 1. The generator, widened ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION gen_stocktake_txn_code(ts timestamptz DEFAULT now())
RETURNS text AS $$
DECLARE
  day_part text := to_char(ts AT TIME ZONE 'Asia/Kolkata', 'YYDDD');
  seq      int;
BEGIN
  -- Serialise minting per day, so two concurrent inserts cannot both read MAX
  -- and compute the same sequence. Transaction-scoped: released on commit or
  -- rollback, and only ever contended by another insert on the SAME day.
  PERFORM pg_advisory_xact_lock(hashtext('stocktake_txn_code:' || day_part));

  -- SUBSTRING(... FROM 6) with NO length: a day may hold both widths across the
  -- deploy of this file — 26256999 (3-digit tail) and 262561000 (4-digit) are
  -- both codes for day 26256 — and a FOR 3 would read the second as 100 and hand
  -- out a duplicate. Reading to end of string makes MAX() correct for both.
  SELECT COALESCE(MAX(SUBSTRING(txn_code FROM 6)::int), 0) + 1
    INTO seq
    FROM stocktake_transactions
   WHERE txn_code LIKE day_part || '%';

  IF seq > 9999 THEN
    RAISE EXCEPTION
      'stocktake_transactions has reached 9999 adjustments for % — the 4-digit '
      'daily sequence in txn_code is exhausted. Widen the format (see '
      '109_stocktake_txn_code_widen.sql) rather than reusing a reference number.',
      to_char(ts AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD')
      USING ERRCODE = 'sequence_generator_limit_exceeded';
  END IF;

  RETURN day_part || lpad(seq::text, 4, '0');
END;
$$ LANGUAGE plpgsql VOLATILE;


-- ── 2. Let the column hold nine characters ─────────────────────────────────
-- Both widths, not just the new one: every existing row is 8 and none of them
-- are being rewritten.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'stk_txn_code_shape') THEN
    ALTER TABLE stocktake_transactions DROP CONSTRAINT stk_txn_code_shape;
  END IF;
  ALTER TABLE stocktake_transactions
    ADD CONSTRAINT stk_txn_code_shape CHECK (txn_code ~ '^[0-9]{8,9}$');
END$$;

COMMENT ON COLUMN stocktake_transactions.txn_code IS
    'Display reference: YYDDD + a per-day counter in Asia/Kolkata. 9 characters '
    '(4-digit counter) from 109_stocktake_txn_code_widen.sql; rows written before '
    'it are 8 (3-digit counter) and were deliberately not renumbered. Assigned by '
    'trigger, unique, never the foreign-key target — txn_id remains the key.';

COMMIT;
