-- 083_dev_jc_per_article_dispatch.sql
-- Phase D of the per-article NPD dev job card: per-article OUTPUT + partial-out dispatch.
-- On promote, each article receives its OWN finished FG sample into the R&D location
-- (its own batch), and that article's finalized output can be dispatched out in parts
-- independently — each part its own 265 GI + outpass. Extends 082 (articles) and 078
-- (the dispatch ledger).
-- Applied out-of-band to the live DB (like samples 068-082). Idempotent. Safe to re-run.

-- Each article gets its own FG-sample batch on promote (receive_fg_sample keys the batch
-- by reference_id = article_id, so batches don't collide across a card's articles).
ALTER TABLE npd_dev_job_card_articles
    ADD COLUMN IF NOT EXISTS fg_sample_batch_id TEXT;

-- A dispatch part belongs to one article (NULL = a legacy card-level dispatch, pre-Phase-D
-- / single-product card). The per-article remaining balance = article.output_qty − Σ its
-- dispatch qty; the SELECT … FOR UPDATE on the parent card still serializes the sum-check.
ALTER TABLE npd_dev_dispatch
    ADD COLUMN IF NOT EXISTS article_id BIGINT
        REFERENCES npd_dev_job_card_articles(article_id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_npd_dev_dispatch_article ON npd_dev_dispatch(article_id);
