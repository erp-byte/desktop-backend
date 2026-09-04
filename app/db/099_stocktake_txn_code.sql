-- 099_stocktake_txn_code.sql — human-quotable 8-digit reference for the ledger.
--
-- WHAT THIS ADDS
-- `stocktake_transactions.txn_code`: exactly 8 digits, YYMMDD + a 2-digit
-- per-day sequence, e.g. the first adjustment posted on 2026-09-04 is 26090401.
-- This is the number the UI shows and an operator quotes on a phone call.
--
-- IT IS A DISPLAY KEY, NOT THE KEY. txn_id stays the PRIMARY KEY and stays the
-- target of reverses_txn_id, because a correction chain must not depend on a
-- format decision. Nothing in the schema is re-pointed at txn_code; it is
-- UNIQUE so it can be looked up, and that is all it is for.
--
-- WHY THE SEQUENCE IS PER DAY
-- The date is the readable half; the sequence only has to disambiguate within
-- it. That caps a single day at 99 adjustments. See the RAISE in
-- gen_stocktake_txn_code(): the cap FAILS LOUDLY rather than wrapping into a
-- duplicate, because a ledger that silently reuses a reference number is worse
-- than one that refuses a row. If 99/day is ever reached, widen the sequence to
-- 3 digits (YYDDD + NNN keeps 8 digits and gives 999/day) — a format change is
-- cheap now and expensive once codes have been quoted on paperwork.
--
-- TIMEZONE. to_char() on a timestamptz renders in the SERVER's TimeZone, which
-- is the same setting `created_at::date` uses in every filter in
-- transactions_service._ledger_filters. So the date inside a code always agrees
-- with the date filter that finds it. Changing the server timezone would break
-- that agreement for rows already minted; don't.
--
-- Idempotent: the whole file re-executes on every scripts/migrate.py run.

-- ── 1. The column ──────────────────────────────────────────────────────────
-- Nullable and unconstrained at first: rows already in the table have no code
-- yet, and both NOT NULL and the shape CHECK would reject them. Tightened in
-- step 4, after the backfill.
ALTER TABLE stocktake_transactions
    ADD COLUMN IF NOT EXISTS txn_code TEXT;


-- ── 2. The generator ───────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION gen_stocktake_txn_code(ts timestamptz DEFAULT now())
RETURNS text AS $$
DECLARE
  day_part text := to_char(ts, 'YYMMDD');
  seq      int;
BEGIN
  -- Serialise minting per day. Two concurrent inserts would otherwise both read
  -- MAX = N and both compute N+1; one would then die on the unique index with a
  -- constraint error instead of getting the next number. The lock is
  -- transaction-scoped, so it is released whether the insert commits or rolls
  -- back, and it only ever contends with another insert on the SAME day.
  PERFORM pg_advisory_xact_lock(hashtext('stocktake_txn_code:' || day_part));

  SELECT COALESCE(MAX(SUBSTRING(txn_code FROM 7 FOR 2)::int), 0) + 1
    INTO seq
    FROM stocktake_transactions
   WHERE txn_code LIKE day_part || '%';

  IF seq > 99 THEN
    RAISE EXCEPTION
      'stocktake_transactions has reached 99 adjustments for % — the 2-digit '
      'daily sequence in txn_code is exhausted. Widen the format (see '
      '099_stocktake_txn_code.sql) rather than reusing a reference number.',
      to_char(ts, 'YYYY-MM-DD');
  END IF;

  RETURN day_part || lpad(seq::text, 2, '0');
END;
$$ LANGUAGE plpgsql VOLATILE;


-- ── 3. Assign on insert, in the database ───────────────────────────────────
-- A trigger rather than application code: 098 already puts the append-only rule
-- in the database, and a reference number that could be omitted by whichever
-- client wrote the row is not a reference number. NEW.created_at is already
-- populated here — column DEFAULTs are applied before BEFORE INSERT triggers —
-- so the code's date always matches the row's own timestamp.
CREATE OR REPLACE FUNCTION stocktake_txn_assign_code() RETURNS trigger AS $$
BEGIN
  IF NEW.txn_code IS NULL THEN
    NEW.txn_code := gen_stocktake_txn_code(COALESCE(NEW.created_at, now()));
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
                  WHERE tgname = 'trg_stk_txn_assign_code'
                    AND tgrelid = 'stocktake_transactions'::regclass) THEN
    CREATE TRIGGER trg_stk_txn_assign_code
      BEFORE INSERT ON stocktake_transactions
      FOR EACH ROW EXECUTE FUNCTION stocktake_txn_assign_code();
  END IF;
END$$;


-- ── 4. Backfill the rows that predate the column ───────────────────────────
-- THIS NEEDS THE APPEND-ONLY UPDATE TRIGGER TEMPORARILY OFF. 098 blocks UPDATE
-- on this table by design, and that block does not distinguish "rewriting
-- history" from "populating a column that did not exist when the row was
-- written". Nothing about the ledger's meaning changes here: only txn_code —
-- NULL on every affected row — is assigned, and the WHERE clause makes the
-- statement a no-op on re-run. DELETE stays blocked throughout.
--
-- If any statement below fails, migrate.py's implicit transaction rolls the
-- whole file back, trigger state included, so the table cannot be left
-- writable by a partial run.
DO $$
DECLARE
  busiest int;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM stocktake_transactions WHERE txn_code IS NULL) THEN
    RETURN;  -- already backfilled; skip the trigger dance entirely
  END IF;

  -- Refuse rather than emit a 9-character code if history has a day over 99.
  SELECT MAX(c) INTO busiest
    FROM (SELECT COUNT(*) AS c FROM stocktake_transactions
           WHERE txn_code IS NULL GROUP BY created_at::date) d;
  IF busiest > 99 THEN
    RAISE EXCEPTION
      'Cannot backfill txn_code: one day holds % existing transactions, more '
      'than the 2-digit daily sequence can number. Widen the format first.',
      busiest;
  END IF;

  EXECUTE 'ALTER TABLE stocktake_transactions DISABLE TRIGGER trg_stk_txn_no_update';

  -- Numbered by txn_id within each day, so the codes run in the same order the
  -- rows were actually posted.
  WITH numbered AS (
    SELECT txn_id,
           to_char(created_at, 'YYMMDD') AS day_part,
           ROW_NUMBER() OVER (PARTITION BY created_at::date ORDER BY txn_id) AS n
      FROM stocktake_transactions
     WHERE txn_code IS NULL
  )
  UPDATE stocktake_transactions t
     SET txn_code = numbered.day_part || lpad(numbered.n::text, 2, '0')
    FROM numbered
   WHERE t.txn_id = numbered.txn_id;

  EXECUTE 'ALTER TABLE stocktake_transactions ENABLE TRIGGER trg_stk_txn_no_update';
END$$;


-- ── 5. Now that every row has one, constrain it ────────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'stk_txn_code_shape') THEN
    ALTER TABLE stocktake_transactions
      ADD CONSTRAINT stk_txn_code_shape CHECK (txn_code ~ '^[0-9]{8}$');
  END IF;
END$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_stk_txn_code
    ON stocktake_transactions (txn_code);

-- NOT NULL last: it is only true once the backfill above has run, and stays
-- true because the BEFORE INSERT trigger fills it for everything new.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_name = 'stocktake_transactions'
                AND column_name = 'txn_code'
                AND is_nullable = 'YES')
     AND NOT EXISTS (SELECT 1 FROM stocktake_transactions WHERE txn_code IS NULL) THEN
    ALTER TABLE stocktake_transactions ALTER COLUMN txn_code SET NOT NULL;
  END IF;
END$$;

COMMENT ON COLUMN stocktake_transactions.txn_code IS
    'Display reference: 8 digits, YYMMDD + 2-digit daily sequence (e.g. 26090401). '
    'Assigned by trigger, unique, never the foreign-key target — txn_id remains the key.';
