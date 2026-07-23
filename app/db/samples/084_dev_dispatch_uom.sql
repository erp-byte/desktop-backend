-- 084_dev_dispatch_uom.sql
-- Per-dispatch unit of measure. Each partial out of an article's FG sample can now be
-- issued in a custom unit + quantity (e.g. kg, g, pcs, box) — the operator picks it on
-- the dispatch form and it prints on that part's outpass. NULL falls back to the
-- article's output_uom (the previous behaviour). Extends 083.
-- Applied out-of-band to the live DB (like samples 068-083). Idempotent. Safe to re-run.
ALTER TABLE npd_dev_dispatch
    ADD COLUMN IF NOT EXISTS uom TEXT;
