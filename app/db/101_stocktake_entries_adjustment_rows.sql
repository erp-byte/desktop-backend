-- 101_stocktake_entries_adjustment_rows.sql
-- Let a console adjustment write a row into stocktake_entries WITHOUT that row
-- being mistaken for a physical count.
--
-- WHY A NEW source_kind AND NOT JUST source_kind='COUNT'
-- The console's stock figure resolves a baseline as MAX(day) over
-- stocktake_entries. If an adjustment row is indistinguishable from a count,
-- then an adjustment posted on a day nobody counted becomes the newest "count
-- day" — and the view collapses to that one article while every genuinely
-- counted article disappears. Reproduced against live data before writing this:
--     before   as_of=2026-09-04  items=2   weight=5.030
--     after    as_of=2026-09-05  items=1   weight=42.500
-- A discriminator column is what lets the baseline ignore adjustments, so this
-- constraint change is a correctness prerequisite, not tidiness.
--
-- WHY NOT source_kind='MOVEMENT'
-- That value already exists and would have been the natural home, but it is
-- bound by chk_entries_source to a NOT NULL movement_id -> stock_movements(id),
-- and stock_movements requires daily_position_id -> stock_daily_position. Both
-- of those tables exist in this database with ZERO rows and ZERO references
-- anywhere in the monorepo (grepped: Stock_Take, server_replica, web_replica,
-- legacy_*). They are orphaned schema. Adopting them would mean building and
-- owning that entire stack; ADJUSTMENT keeps this change proportionate. If the
-- movement machinery is ever switched on, adjustment rows can be migrated into
-- it — that is why this reuses source_kind rather than inventing a new column.
--
-- Idempotent: the whole file re-executes on every scripts/migrate.py run.

-- ── 1. Widen the source vocabulary ─────────────────────────────────────────
-- Guarded so the constraint is not dropped and revalidated on every deploy —
-- ADD CONSTRAINT rescans the table, and a window where the CHECK is absent is
-- a window where a bad row can land. Inside migrate.py's implicit transaction
-- the drop+add is atomic, so no such window is ever visible to another session.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_entries_source'
           AND conrelid = 'stocktake_entries'::regclass
           AND pg_get_constraintdef(oid) LIKE '%ADJUSTMENT%')
    THEN
        ALTER TABLE stocktake_entries DROP CONSTRAINT IF EXISTS chk_entries_source;
        ALTER TABLE stocktake_entries ADD CONSTRAINT chk_entries_source CHECK (
               (source_kind = 'COUNT'      AND movement_id IS NULL)
            OR (source_kind = 'ADJUSTMENT' AND movement_id IS NULL)
            OR (source_kind = 'MOVEMENT'   AND movement_id IS NOT NULL)
        );
    END IF;
END$$;

-- ── 2. One adjustment row per article, per place, per IST day ──────────────
-- This is the "does today's entry already exist?" rule, enforced by the
-- database rather than by a read-then-write in the service. With it, the write
-- back is a single INSERT ... ON CONFLICT DO UPDATE and two concurrent
-- adjustments for the same article cannot both create a row.
--
-- THE DAY EXPRESSION MUST BE THE TWO-STEP FORM. created_at is `timestamp
-- WITHOUT time zone` holding UTC, so it has to be told what it is
-- (AT TIME ZONE 'UTC') before being converted to the warehouse's day. That is
-- also the only form Postgres will accept here: the one-step
-- (created_at AT TIME ZONE 'Asia/Kolkata')::date is rejected outright with
-- "functions in index expression must be marked IMMUTABLE" — so the correct
-- conversion and the indexable one are the same expression. It matches
-- business_day.ENTRY_DAY exactly; if one changes, so must the other.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entries_adjustment_day
    ON stocktake_entries (
        (((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Kolkata')::date),
        UPPER(BTRIM(item_name)),
        UPPER(BTRIM(warehouse)),
        UPPER(BTRIM(floor_name)),
        stock_type)
    WHERE source_kind = 'ADJUSTMENT';

-- Counts are untouched by that index (it is partial), so the floor app's
-- existing behaviour — many count rows for the same article on the same day —
-- keeps working exactly as before.

COMMENT ON COLUMN stocktake_entries.source_kind IS
    'COUNT = a physical count entered on the floor. ADJUSTMENT = a console stock '
    'adjustment written back from stocktake_transactions; one row per article per '
    'place per IST day, holding the signed delta. MOVEMENT = reserved for the '
    'stock_movements machinery (unused). Baseline count-date resolution must '
    'consider COUNT rows ONLY.';
