-- 110_stocktake_txn_verification.sql — sign-off on the individual transaction.
--
-- WHAT CHANGES, AND WHAT IT CONTRADICTS
-- Until now a sign-off lived only on new_stock_entries, and 098, 108 and
-- transactions_service all say why in so many words:
--
--   "stocktake_transactions is append-only (trg_stk_txn_no_update raises on
--    UPDATE), so it could never have held a mutable flag"
--                                  — 108_stock_take_verification_role.sql
--
-- That was true of the trigger as written, not of the ledger as a concept. The
-- guarantee anyone actually depends on is that A POSTING NEVER CHANGES: the
-- quantity, the article, the place, the operator and the time are final, and a
-- mistake is corrected by a reversal. Whether somebody has since checked that
-- posting is not part of the posting. This file separates those two ideas —
-- the facts stay frozen, the sign-off becomes mutable — and narrows the trigger
-- to enforce exactly that.
--
-- HOW THE NARROWING IS DONE, AND WHY NOT A COLUMN LIST
-- The obvious implementation compares NEW.item_name <> OLD.item_name and so on
-- for every frozen column. That is a list somebody has to remember to extend,
-- and the failure mode is silent: a column added in 2027 would be quietly
-- mutable forever. Instead the check subtracts the three verification keys from
-- the whole row and compares what is left:
--
--     to_jsonb(NEW) - 'verified' - 'verified_by' - 'verified_at'
--       IS DISTINCT FROM
--     to_jsonb(OLD) - 'verified' - 'verified_by' - 'verified_at'
--
-- so every column that exists now, and every column added later, is frozen by
-- default and can only be unfrozen deliberately. DELETE stays blocked outright.
--
-- ONE THING THIS GIVES UP, ON PURPOSE. The old trigger refused every UPDATE, so
-- even `SET reason = reason` raised. This one compares VALUES, so an update that
-- writes a column's existing value back is allowed — it changes nothing, which
-- is exactly what the rule is there to guarantee. The stricter reading would
-- need `BEFORE UPDATE OF <column list>`, since a BEFORE ROW trigger cannot see
-- which columns a statement named, only what they now hold — and that is the
-- hand-maintained list this file exists to avoid. So the guarantee is "a posting
-- never changes", not "nobody may address this table with the word UPDATE".
-- tests/services/test_stock_take_verification_live_sql.py asserts both halves.
--
-- THE BACKFILL NEEDS NO TRIGGER DANCE
-- 099 and 100 had to DISABLE TRIGGER to populate txn_code, and both files are
-- uneasy about it. This one does not: it writes only the three verification
-- columns, which is precisely what the narrowed trigger now permits. That the
-- backfill runs at all is the proof the narrowing works.
--
-- EXISTING ROWS INHERIT WHAT THEY ALREADY SHOWED. Every transaction already
-- displayed a verification — read back from the adjustment row it rolls into,
-- on the key uq_nse_adjustment_day enforces. The backfill copies exactly that,
-- so nothing on any screen changes the moment this runs; 889 of the 1018 rows
-- are already verified through that join and stay verified after it.
--
-- WAREHOUSE CODES ARE NORMALISED ON THE LEDGER SIDE ONLY. The ledger holds both
-- 'W-202' and 'W202'; new_stock_entries holds only the unhyphenated form. The
-- REPLACE below mirrors transactions_service._attach_verification exactly, so
-- the backfill reproduces the join that was already being displayed rather than
-- a subtly different one.
--
-- Idempotent: the whole file re-executes on every scripts/migrate.py run.

BEGIN;

-- ── 1. The columns ─────────────────────────────────────────────────────────
ALTER TABLE stocktake_transactions
    ADD COLUMN IF NOT EXISTS verified    boolean NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS verified_by text,
    ADD COLUMN IF NOT EXISTS verified_at timestamptz;

-- All three move together or none do. Without this an un-verify could leave the
-- name of whoever last signed it sitting on an unverified row, which reads as an
-- accusation.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_stk_txn_verified_consistent') THEN
        ALTER TABLE stocktake_transactions
          ADD CONSTRAINT chk_stk_txn_verified_consistent CHECK (
              (verified     AND verified_by IS NOT NULL AND verified_at IS NOT NULL)
           OR (NOT verified AND verified_by IS NULL     AND verified_at IS NULL));
    END IF;
END$$;


-- ── 2. Narrow the append-only trigger ──────────────────────────────────────
-- A NEW FUNCTION, AND THE TRIGGERS RE-POINTED AT IT. The obvious move is to
-- CREATE OR REPLACE stocktake_txn_block_write() in place and let 098's triggers
-- pick the new body up. That works exactly once. migrate.py runs every file in
-- order on every run, 098 sits above this one in the list, and 098 replaces that
-- function UNCONDITIONALLY with the hard block:
--
--     098   CREATE OR REPLACE FUNCTION stocktake_txn_block_write()  -- raises always
--     ...
--     110   CREATE OR REPLACE FUNCTION stocktake_txn_block_write()  -- narrowed
--
-- so every run re-blocks the table and then re-narrows it, and a run that fails
-- anywhere in between — 101 through 109, any one of them — leaves production
-- with a table that refuses every sign-off, with nothing to say why. The window
-- is small and the failure is silent and total, which is the worst combination.
--
-- So the narrowed rule lives in its own function that 098 does not know about,
-- and this file owns the triggers. 098's guard is `IF NOT EXISTS (SELECT 1 FROM
-- pg_trigger WHERE tgname = ...)`, so once these exist under the same names it
-- skips creating them and its own function object is left defined but
-- unreferenced. Re-running either file in any order converges on this.
CREATE OR REPLACE FUNCTION stocktake_txn_guard_write() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Everything except the three verification keys must be identical.
        -- Subtraction rather than a column list: see the header. IS DISTINCT
        -- FROM, not <>, so a NULL anywhere in the row cannot make the
        -- comparison NULL and let a real edit through.
        IF (to_jsonb(NEW) - 'verified' - 'verified_by' - 'verified_at')
           IS DISTINCT FROM
           (to_jsonb(OLD) - 'verified' - 'verified_by' - 'verified_at') THEN
            RAISE EXCEPTION
                'stocktake_transactions is append-only: a posting cannot be '
                'edited. Post a reversal instead. (Only verified, verified_by '
                'and verified_at may be updated.)'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'stocktake_transactions is append-only: % is blocked. Post a reversal instead.',
        TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

-- Re-point 098's triggers. Names are kept so anything checking them by name --
-- tests/services/test_stocktake_transactions_live_sql.py asserts all three
-- triggers on this table are enabled -- keeps working, and so that 098's
-- IF NOT EXISTS guard finds them and leaves them alone on later runs.
DROP TRIGGER IF EXISTS trg_stk_txn_no_update ON stocktake_transactions;
CREATE TRIGGER trg_stk_txn_no_update
    BEFORE UPDATE ON stocktake_transactions
    FOR EACH ROW EXECUTE FUNCTION stocktake_txn_guard_write();

DROP TRIGGER IF EXISTS trg_stk_txn_no_delete ON stocktake_transactions;
CREATE TRIGGER trg_stk_txn_no_delete
    BEFORE DELETE ON stocktake_transactions
    FOR EACH ROW EXECUTE FUNCTION stocktake_txn_guard_write();

COMMENT ON FUNCTION stocktake_txn_block_write() IS
    'SUPERSEDED by stocktake_txn_guard_write() in 110_stocktake_txn_verification.sql. '
    '098 still recreates this on every migrate run, but no trigger references it: '
    'it raises on every UPDATE, which would block the sign-off columns.';


-- ── 3. Backfill from the adjustment row each transaction rolls into ────────
-- WHERE NOT verified makes it a no-op on re-run, and stops a later run from
-- re-stamping a sign-off that a person has since changed by hand.
UPDATE stocktake_transactions t
   SET verified    = TRUE,
       verified_by = v.verified_by,
       verified_at = COALESCE(v.verified_at, t.created_at)
  FROM (
      SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date   AS k_day,
             UPPER(BTRIM(item_name))                          AS k_item,
             UPPER(BTRIM(COALESCE(warehouse, '')))            AS k_wh,
             UPPER(BTRIM(COALESCE(floor_name, '')))           AS k_fl,
             COALESCE(stock_type, 'Fresh Stock')              AS k_stock,
             BOOL_AND(COALESCE(verified, FALSE))              AS verified,
             MAX(verified_by)                                 AS verified_by,
             MAX(verified_at)                                 AS verified_at
        FROM new_stock_entries
       WHERE source_kind = 'ADJUSTMENT'
       GROUP BY 1, 2, 3, 4, 5
  ) v
 WHERE NOT t.verified
   AND v.verified
   AND v.verified_by IS NOT NULL
   AND v.k_day   = (t.created_at AT TIME ZONE 'Asia/Kolkata')::date
   AND v.k_item  = UPPER(BTRIM(t.item_name))
   AND v.k_wh    = REPLACE(UPPER(BTRIM(COALESCE(t.warehouse, ''))), '-', '')
   AND v.k_fl    = UPPER(BTRIM(COALESCE(t.location, '')))
   AND v.k_stock = COALESCE(t.stock_type, 'Fresh Stock');


-- ── 4. The index the reconciliation reads ──────────────────────────────────
-- Both directions group by this key: "is every transaction in this group
-- verified" and "verify every transaction in this group". It must match
-- transactions_service.TXN_GROUP_KEY character for character, or neither query
-- uses it.
CREATE INDEX IF NOT EXISTS idx_stk_txn_verif_group
    ON stocktake_transactions (
        ((created_at AT TIME ZONE 'Asia/Kolkata')::date),
        UPPER(BTRIM(item_name)),
        REPLACE(UPPER(BTRIM(COALESCE(warehouse, ''))), '-', ''),
        UPPER(BTRIM(COALESCE(location, ''))),
        COALESCE(stock_type, 'Fresh Stock'));


COMMENT ON COLUMN stocktake_transactions.verified IS
    'Whether someone with the verify permission has signed THIS posting off. '
    'Mutable -- the one part of the row that is. Kept in step with the '
    'new_stock_entries adjustment row by transactions_service: the row is '
    'verified when every transaction in its (IST day, article, warehouse, floor, '
    'stock type) group is.';

COMMENT ON TABLE stocktake_transactions IS
    'Append-only stock adjustment ledger over new_stock_entries. THE POSTING is '
    'final -- quantity, article, place, operator and time can never change, and a '
    'mistake is corrected by a new row with reverses_txn_id set. DELETE is blocked '
    'outright; UPDATE is blocked for every column except verified / verified_by / '
    'verified_at, which carry the sign-off and are meant to change. See '
    '110_stocktake_txn_verification.sql.';

COMMIT;
