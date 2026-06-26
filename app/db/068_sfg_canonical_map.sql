-- ===========================================================================
-- 068_sfg_canonical_map.sql — persisted FG (article) → canonical SFG catalogue.
-- Source: data/sfg_canonical_map.csv (SFG canonicalization design, §4–§5.4).
-- One row per FG; sfg_name NULL = NO_SFG (plain repack / hamper / RM stock item).
-- Loaded idempotently by ingest_canonical_map() (sfg_canonical.py), and read by
-- the Create/Edit Job-Card SFG auto-fill (get_line_job_card_config).
-- Standalone reference table (no FK); safe to re-run.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS sfg_canonical_map (
    entity        TEXT NOT NULL,                  -- cfpl | cdpl
    fg_name       TEXT NOT NULL,                  -- FG (article) name as in the BOM / all_sku
    fg_norm       TEXT NOT NULL,                  -- normalise_key(fg_name).lower() — lookup key
    fg_salegroup  TEXT,                           -- Bulk | RPC | VA | DATES … (authoritative classifier)
    case_label    TEXT,                           -- SFG_mint_va | NO_SFG_plain_repack | SFG_blend | …
    sfg_name      TEXT,                           -- canonical SFG name; NULL ⇒ NO_SFG
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_sfg_canonical_map UNIQUE (entity, fg_norm)
);

CREATE INDEX IF NOT EXISTS idx_sfg_canonical_map_fgnorm ON sfg_canonical_map(fg_norm);

COMMENT ON TABLE sfg_canonical_map IS
  'FG (article) -> canonical SFG name catalogue (SFG canonicalization design). '
  'sfg_name NULL = NO_SFG (plain repack/hamper/RM). Source: data/sfg_canonical_map.csv.';
