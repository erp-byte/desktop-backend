-- 035_jc_additives.sql
--
-- Job-card additive consumption (data-keeping bucket).
--
-- The shop floor records seasoning that goes INTO a batch but produces
-- no measurable output: salt, sugar, citric acid, gum powder, edible
-- oils, cayenne pepper, etc. These are 100 % consumed by intent, so
-- they would create artificial unbalance if they fed the conservation
-- identity. Per operator policy, they're tracked here purely for
-- consumption history + future commercial / costing reconciliation.
--
-- IMPORTANT: rows in this table are NEVER summed into total_accounted
-- on the server side (jc_accounting_v2). The frontend surfaces the
-- total in the Accounting Summary as a separate "Additives" KV labelled
-- "data only · not in balance".
--
-- A row carries EITHER:
--   • sku_name      — populated when the operator picked an entry from
--                     the all_sku-backed dropdown (canonical path)
--   • material_name — populated only when the operator chose "Others"
--                     and free-typed the name (escape hatch for SKUs
--                     not yet in the catalog)
-- The CHECK constraint enforces "at least one identifier must be set"
-- so a row can't silently lose its provenance.

CREATE TABLE IF NOT EXISTS job_card_additive_consumption_v2 (
    -- App-supplied 8-digit time-based BIGINT (last 8 digits of epoch
    -- millis) — see app.core.helpers.new_short_time_id and the matching
    -- so_line / so_fulfillment_v2 / byproduct_id pattern. Not auto-
    -- incremented; insert_with_pk_retry handles the rare epoch-ms
    -- collision by regenerating the candidate and re-inserting.
    additive_id    BIGINT PRIMARY KEY,
    job_card_id    BIGINT NOT NULL
                   REFERENCES job_card_v2(job_card_id) ON DELETE CASCADE,
    sku_name       TEXT,                           -- from all_sku.particulars
    material_name  TEXT,                           -- free-text "Others" fallback
    qty_kg         NUMERIC(15,3) NOT NULL CHECK (qty_kg >= 0),
    remarks        TEXT,
    recorded_by    TEXT,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_additive_has_name CHECK (
        sku_name IS NOT NULL OR material_name IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_jc_additives_v2_jc
    ON job_card_additive_consumption_v2(job_card_id);

COMMENT ON COLUMN job_card_additive_consumption_v2.additive_id IS
    '8-digit time-based BIGINT supplied by app.core.helpers.new_short_time_id; '
    'insert_with_pk_retry handles epoch-ms collisions.';

COMMENT ON TABLE job_card_additive_consumption_v2 IS
    'Data-keeping consumption for fully-consumed seasoning (salt, sugar, '
    'citric acid, gum powder, oils, cayenne pepper, etc.). Does NOT '
    'participate in the conservation identity — surfaced separately in '
    'the Accounting Summary so the operator can review consumption '
    'history without affecting balance closure.';
