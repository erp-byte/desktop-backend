-- 100_stocktake_txn_code_format.sql — widen txn_code and cut its date in IST.
--
-- SUPERSEDES THE FORMAT INTRODUCED BY 099. Still exactly 8 digits, but:
--
--     was   YYMMDD + NN    99 per day, day cut in the server's timezone (UTC)
--     now   YYDDD  + NNN   999 per day, day cut in Asia/Kolkata
--
-- YYDDD is the 2-digit year plus the day-of-year, so 2026-09-04 is 26247 and the
-- first adjustment that day is 26247001. The month/day is no longer readable at a
-- glance; that is the price of a third sequence digit inside 8 characters, and
-- 99/day was too tight a ceiling to leave in a ledger that must never refuse a
-- real stock movement.
--
-- WHY IST. The RDS server runs on UTC, so 099's to_char() cut the day at 05:30
-- IST: an adjustment posted at 1am on the 5th was stamped 260904 while the screen
-- — rendering the same instant in the browser's timezone — said the 5th. The
-- warehouse works to Asia/Kolkata, so the code, the ledger's date filters, the
-- baseline count date and the netting window all now use that day. See
-- app/modules/stock_take/services/business_day.py, which holds the matching SQL
-- for the read side, and note that stocktake_entries.created_at is a NAIVE
-- column holding UTC and so needs a different (two-step) conversion.
--
-- 099 IS DELIBERATELY LEFT AS IT WAS. It is the honest record of what already ran
-- against warehouse_db, and this file reconciles that database with the current
-- format. On a database built fresh from app/db/, 099 creates the column, the
-- trigger and the function, and this file immediately replaces the function and
-- finds no legacy rows to convert.
--
-- Idempotent: the whole file re-executes on every scripts/migrate.py run.

-- ── 1. The generator, widened and moved to IST ─────────────────────────────
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

  SELECT COALESCE(MAX(SUBSTRING(txn_code FROM 6 FOR 3)::int), 0) + 1
    INTO seq
    FROM stocktake_transactions
   WHERE txn_code LIKE day_part || '%';

  IF seq > 999 THEN
    RAISE EXCEPTION
      'stocktake_transactions has reached 999 adjustments for % — the 3-digit '
      'daily sequence in txn_code is exhausted. Widen the format rather than '
      'reusing a reference number.',
      to_char(ts AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD');
  END IF;

  RETURN day_part || lpad(seq::text, 3, '0');
END;
$$ LANGUAGE plpgsql VOLATILE;


-- ── 2. Convert codes still in 099's format ─────────────────────────────────
-- A row is legacy iff its first 5 digits are not the YYDDD its own timestamp
-- would produce. That test is exact and self-correcting: it cannot be fooled by
-- the two formats sharing a digit count (26090301 is a valid-LOOKING YYDDD+NNN),
-- because a correctly minted code always agrees with its own row, and a legacy
-- one never can — 099 wrote a month there, not a day-of-year.
--
-- Needs 098's append-only UPDATE trigger off, for the same reason 099's backfill
-- did: that block cannot tell a reference-number reformat from a rewrite of
-- history. Only txn_code changes; DELETE stays blocked. If anything below
-- fails, migrate.py's implicit transaction rolls back the trigger state too.
DO $$
DECLARE
  legacy  int;
  busiest int;
BEGIN
  SELECT COUNT(*) INTO legacy
    FROM stocktake_transactions
   WHERE SUBSTRING(txn_code FROM 1 FOR 5)
         <> to_char(created_at AT TIME ZONE 'Asia/Kolkata', 'YYDDD');
  IF legacy = 0 THEN
    RETURN;  -- already converted (or a fresh database with no rows)
  END IF;

  SELECT MAX(c) INTO busiest FROM (
    SELECT COUNT(*) AS c FROM stocktake_transactions
     GROUP BY (created_at AT TIME ZONE 'Asia/Kolkata')::date) d;
  IF busiest > 999 THEN
    RAISE EXCEPTION
      'Cannot renumber txn_code: one day holds % transactions, more than the '
      '3-digit daily sequence can number.', busiest;
  END IF;

  EXECUTE 'ALTER TABLE stocktake_transactions DISABLE TRIGGER trg_stk_txn_no_update';

  -- EVERY row is renumbered, not just the legacy ones: the sequence has to be
  -- contiguous within each IST day, and a day can mix rows that were already
  -- converted with rows that were not. Ordering by txn_id keeps the codes in the
  -- order the rows were actually posted.
  WITH numbered AS (
    SELECT txn_id,
           to_char(created_at AT TIME ZONE 'Asia/Kolkata', 'YYDDD') AS day_part,
           ROW_NUMBER() OVER (PARTITION BY (created_at AT TIME ZONE 'Asia/Kolkata')::date
                                  ORDER BY txn_id) AS n
      FROM stocktake_transactions
  )
  UPDATE stocktake_transactions t
     SET txn_code = numbered.day_part || lpad(numbered.n::text, 3, '0')
    FROM numbered
   WHERE t.txn_id = numbered.txn_id
     AND t.txn_code IS DISTINCT FROM numbered.day_part || lpad(numbered.n::text, 3, '0');

  EXECUTE 'ALTER TABLE stocktake_transactions ENABLE TRIGGER trg_stk_txn_no_update';

  RAISE NOTICE 'txn_code: renumbered % legacy row(s) to YYDDD+NNN', legacy;
END$$;

COMMENT ON COLUMN stocktake_transactions.txn_code IS
    'Display reference: 8 digits, YYDDD + 3-digit daily sequence in Asia/Kolkata '
    '(e.g. 26247001 = 2026 day 247, first adjustment). Assigned by trigger, '
    'unique, never the foreign-key target — txn_id remains the key.';
