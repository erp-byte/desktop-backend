-- 098_stocktake_transactions.sql — append-only stock adjustment ledger.
--
-- WHAT THIS IS
-- One row per physical stock movement recorded between counts: an ADDITION or a
-- SUBTRACTION against an article at one warehouse+floor, with a mandatory reason.
-- A posted row is FINAL. Corrections are new rows pointing at what they correct
-- (reverses_txn_id), never edits.
--
-- WHY THE ARTICLE IS A STRING, NOT A KEY
-- This ledger nets against `stocktake_entries`, whose item_name is a bare
-- VARCHAR(255) with no FK and no sku_id on any of its three INSERT paths — and
-- the Stock Take floor UI deliberately ships "Other (custom item)" free-text
-- hatches, so unmatched names are designed behaviour, not corruption. Stock
-- identity across this system is therefore UPPER(TRIM(item_name)) plus stock_type
-- — the literal GROUP BY of both latest-stock implementations. We store the
-- normalised name (the only thing that joins) AND sku_id when the operator picked
-- from the catalogue (the audit trail). sku_id IS NULL honestly encodes "operator
-- created" or "counted name absent from the master"; all_sku.particulars has no
-- UNIQUE constraint so it could not be an FK target anyway.
--
-- FLOOR VOCABULARY WARNING
-- `location` holds the floor AS GRANTED in auth_user.allowed_floors (Title Case,
-- e.g. 'Upper Basement'), while stocktake_entries.floor_name is uppercase and
-- frequently carries trailing spaces ('UPPER BASEMENT '). Every join between the
-- two MUST apply UPPER(BTRIM(...)) to both sides. Note only ~39% of counted rows
-- sit on a floor that is granted to anybody today, so netting coverage is limited
-- by the grant data, not by this schema.
--
-- Idempotent: the whole file re-executes on every scripts/migrate.py run.

CREATE TABLE IF NOT EXISTS stocktake_transactions (
    txn_id             BIGSERIAL     PRIMARY KEY,

    -- ── Article identity ────────────────────────────────────────────────────
    item_name          TEXT          NOT NULL,   -- stored UPPER(BTRIM(...)); joins stocktake_entries.item_name
    sku_id             INT           NULL,       -- all_sku.sku_id when picked from the catalogue; see note below
    is_new_article     BOOLEAN       NOT NULL DEFAULT FALSE,

    material_type      TEXT          NOT NULL,   -- all_sku.item_type
    item_category      TEXT          NOT NULL,   -- all_sku.item_group
    item_subcategory   TEXT          NOT NULL,   -- all_sku.sub_group
    -- Second half of the stocktake_entries identity. Fresh Stock and Off
    -- Grade/Rejection are different stock and are never summed together.
    stock_type         TEXT          NOT NULL DEFAULT 'Fresh Stock',

    -- ── Quantities ──────────────────────────────────────────────────────────
    -- units and qty_kg are BOTH operator-entered and stored exactly as given.
    -- No relationship is enforced between them: this is a deliberate decision,
    -- matching how stocktake_entries actually behaves (its own
    -- total_weight = total_quantity * unit_uom invariant is unenforced and
    -- already violated on three code paths). Do not add a derivation CHECK here
    -- without also fixing that.
    units              NUMERIC(15,3) NOT NULL CHECK (units > 0),
    qty_kg             NUMERIC(15,3) NOT NULL CHECK (qty_kg > 0),

    -- Direction lives in `operation`; magnitudes are ALWAYS positive. Mirrors
    -- material_document's create_reversal, which copies quantity_kg through
    -- unchanged and expresses direction via the movement type.
    operation          TEXT          NOT NULL CHECK (operation IN ('ADDITION', 'SUBTRACTION')),
    reason             TEXT          NOT NULL CHECK (BTRIM(reason) <> ''),

    -- ── Scope. Derived server-side from the token, never from the body. ─────
    warehouse          TEXT          NOT NULL CHECK (BTRIM(warehouse) <> ''),
    location           TEXT          NOT NULL CHECK (BTRIM(location) <> ''),

    -- ── Correction chain ────────────────────────────────────────────────────
    reverses_txn_id    BIGINT        NULL REFERENCES stocktake_transactions(txn_id),
    is_reversal        BOOLEAN       NOT NULL DEFAULT FALSE,
    -- A reversal must point at its target and a plain entry must not.
    CONSTRAINT stk_txn_reversal_consistent
        CHECK ((is_reversal AND reverses_txn_id IS NOT NULL)
            OR (NOT is_reversal AND reverses_txn_id IS NULL)),

    -- ── Audit ───────────────────────────────────────────────────────────────
    -- created_by is denormalised display text (the house convention shared by
    -- material_document, floor_movement and production_plan_v2); the user id is
    -- the actual key, because a display name is not one and this is a ledger.
    created_by         TEXT          NOT NULL,
    created_by_user_id INT           NULL,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- FKs added separately and guarded: all_sku and auth_user are created by other
-- files in this runner, and on a partially-built database a hard REFERENCES in
-- the CREATE TABLE would abort the whole migration rather than degrade.
DO $$
BEGIN
    IF to_regclass('all_sku') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'stk_txn_sku_fk') THEN
        ALTER TABLE stocktake_transactions
            ADD CONSTRAINT stk_txn_sku_fk FOREIGN KEY (sku_id) REFERENCES all_sku(sku_id);
    END IF;
    IF to_regclass('auth_user') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'stk_txn_user_fk') THEN
        ALTER TABLE stocktake_transactions
            ADD CONSTRAINT stk_txn_user_fk FOREIGN KEY (created_by_user_id) REFERENCES auth_user(user_id);
    END IF;
END$$;

-- ── Indexes ─────────────────────────────────────────────────────────────────
-- Functional index mirroring idx_all_sku_particulars_norm: every lookup and every
-- join to stocktake_entries goes through UPPER(BTRIM(item_name)).
CREATE INDEX IF NOT EXISTS idx_stk_txn_item_norm
    ON stocktake_transactions (UPPER(BTRIM(item_name)));
-- The netting query filters by scope and by "since the count date".
CREATE INDEX IF NOT EXISTS idx_stk_txn_scope_created
    ON stocktake_transactions (warehouse, location, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stk_txn_created_at
    ON stocktake_transactions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stk_txn_sku
    ON stocktake_transactions (sku_id) WHERE sku_id IS NOT NULL;
-- One reversal per transaction. Partial, so the many NULLs do not collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stk_txn_reverses
    ON stocktake_transactions (reverses_txn_id) WHERE reverses_txn_id IS NOT NULL;

-- ── Append-only, enforced by the DATABASE ───────────────────────────────────
-- Follows 030_vendor_history.sql, the repo's only real enforcement precedent.
-- Deliberately NOT following material_document, whose immutability is a docstring
-- that three sample-module services already violate with UPDATEs.
--
-- DELETE is blocked as well as UPDATE: 030 leaves DELETE open so parent CASCADEs
-- still work, but nothing cascades into this table, so there is no such need.
-- A genuine mistake is corrected by posting a reversal, which is the point.
CREATE OR REPLACE FUNCTION stocktake_txn_block_write() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'stocktake_transactions is append-only: % is blocked. Post a reversal instead.', TG_OP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_stk_txn_no_update'
                      AND tgrelid = 'stocktake_transactions'::regclass) THEN
        CREATE TRIGGER trg_stk_txn_no_update
            BEFORE UPDATE ON stocktake_transactions
            FOR EACH ROW EXECUTE FUNCTION stocktake_txn_block_write();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgname = 'trg_stk_txn_no_delete'
                      AND tgrelid = 'stocktake_transactions'::regclass) THEN
        CREATE TRIGGER trg_stk_txn_no_delete
            BEFORE DELETE ON stocktake_transactions
            FOR EACH ROW EXECUTE FUNCTION stocktake_txn_block_write();
    END IF;
END$$;

COMMENT ON TABLE stocktake_transactions IS
    'Append-only stock adjustment ledger over stocktake_entries. Rows are final; '
    'corrections are new rows with reverses_txn_id set. UPDATE and DELETE are '
    'blocked by trigger.';
