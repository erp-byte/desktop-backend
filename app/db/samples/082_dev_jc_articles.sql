-- 082_dev_jc_articles.sql
-- Per-article NPD dev job cards (Phase A). A dev job card now develops MULTIPLE target
-- articles, each its own product with its own base BOM + trial recipe (and, in later
-- phases, its own output + promoted BOM). Each recipe line belongs to an article
-- (article_id); the card-level fg_sku_name / pcs / weight_per_piece / target_qty /
-- base_bom_id keep MIRRORING article #1 for backward compat. Legacy cards (no article
-- rows) synthesize a single article from the card header on read.
-- Applied out-of-band to the live DB (like samples 068-081). Idempotent.

-- article_id is an app-supplied 8-digit time-based BIGINT (new_short_time_id), the same
-- handle pattern as the card / phase ids — NOT a SERIAL.
CREATE TABLE IF NOT EXISTS npd_dev_job_card_articles (
    article_id          BIGINT PRIMARY KEY,
    dev_jc_id           BIGINT NOT NULL REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,                       -- target product / promoted FG name
    pcs                 NUMERIC,
    weight_per_piece    NUMERIC,
    quantity            NUMERIC,                             -- derived pcs × weight_per_piece
    uom                 TEXT,
    base_bom_id         INT REFERENCES bom_header(bom_id),   -- this article's own base BOM
    base_bom_name       TEXT,
    -- Per-article output + promotion (populated by later phases; nullable now).
    output_qty          NUMERIC(15, 3),
    output_uom          TEXT,
    rm_consumed_qty     NUMERIC(15, 3),
    wastage_qty         NUMERIC(15, 3),
    extra_give_away_qty NUMERIC(15, 3),
    yield_pct           NUMERIC(7, 3),
    promoted_bom_id     INT REFERENCES bom_header(bom_id),
    status              TEXT NOT NULL DEFAULT 'DRAFT',
    line_order          INT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_npd_dev_jc_articles ON npd_dev_job_card_articles(dev_jc_id);

-- Each recipe line belongs to an article (NULL = legacy card-level base recipe). A line
-- keeps its phase_id too, so per-article trial phases can layer on later.
ALTER TABLE npd_dev_job_card_lines
    ADD COLUMN IF NOT EXISTS article_id BIGINT
        REFERENCES npd_dev_job_card_articles(article_id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_npd_dev_jc_lines_article ON npd_dev_job_card_lines(article_id);
