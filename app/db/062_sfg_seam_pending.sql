-- ===========================================================================
-- 062_sfg_seam_pending.sql   (SFG / Job-Card integration — Phase 9 seam minting)
-- ---------------------------------------------------------------------------
-- Phase 9 ("SFG seam minting for transform chains") mints/reuses SFG#### codes on
-- the Create-WIP transform steps that bar-line-routed (061) / gap-promoted (058)
-- FGs grew but that were never seamed: bom_process_route rows with
-- stage_bucket='Create WIP' whose output_code is still NULL/blank. A step that
-- "Creates WIP" but carries no SFG#### output_code has no chain seam — there is
-- nothing for the downstream stage's input_code to consume (the JC1.output_code ->
-- JC2.input_code handoff 050 set up). The minting itself is DATA (opt-in,
-- idempotent script, built by the parallel backend agent — it reuses an existing
-- all_sku.sfg_code where one matches and mints a fresh SFG#### otherwise, then
-- stamps output_code on the step). This migration is the OBSERVABILITY surface:
--
--   * sfg_seam_pending VIEW — the work-queue + post-mint audit: one row per
--     Create-WIP step that STILL lacks an SFG seam (output_code NULL or blank).
--     The minting script targets exactly these rows; the queue drains to EMPTY
--     once every transform chain is seamed, so SELECTing it is the live "are we
--     done?" check. Derived, not stored — adds NO columns to any table.
--   * idx_bom_route_seam_pending — a PARTIAL index on the pending predicate so the
--     view (and the script's per-pass scan) stays cheap and the reversal does not
--     seq-scan bom_process_route. 055's idx_bom_route_output_code is the OPPOSITE
--     predicate (WHERE output_code IS NOT NULL), so it CANNOT serve this NULL-side
--     lookup — this is a genuinely new, non-duplicate index.
--
-- Idempotent (CREATE OR REPLACE VIEW, CREATE INDEX IF NOT EXISTS), forward-only.
-- Reversible via app/db/rollback/sfg_integration_down.sql.
-- (dep: bom_process_route.stage_bucket / output_code / output_kind /
--  practical_operation from 050_sfg_foundation.sql; process_name + step_number +
--  bom_id from production_schema.sql; bom_header.fg_sku_name / entity from
--  production_schema.sql)
-- ===========================================================================
BEGIN;

-- Partial index on the pending predicate so the view and the minting script's
-- per-pass scan stay cheap, and the reversal does not seq-scan bom_process_route.
-- NOT covered by 055's idx_bom_route_output_code (that is WHERE output_code IS NOT
-- NULL — the opposite side); this indexes the NULL-output Create-WIP rows.
CREATE INDEX IF NOT EXISTS idx_bom_route_seam_pending
    ON bom_process_route(bom_id)
    WHERE stage_bucket = 'Create WIP' AND output_code IS NULL;

-- ---------------------------------------------------------------------------
-- sfg_seam_pending — one row per Create-WIP step that still lacks an SFG seam
-- (output_code NULL or blank). This is the Phase 9 minting work-queue AND the
-- post-mint audit: the script seams these rows, and the queue empties once every
-- transform chain carries its SFG#### output_code. The btrim()='' guard also
-- treats whitespace-only stamps as unseamed (matches the partial index, which
-- catches the NULL case the script overwhelmingly produces). Derived, not stored.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW sfg_seam_pending AS
SELECT
    r.bom_id,
    h.fg_sku_name,
    h.entity,
    r.step_number,
    r.process_name,
    r.practical_operation,
    r.stage_bucket,
    r.output_code,
    r.output_kind
FROM bom_process_route r
JOIN bom_header h ON h.bom_id = r.bom_id
WHERE r.stage_bucket = 'Create WIP'
  AND (r.output_code IS NULL OR btrim(r.output_code) = '');

COMMENT ON VIEW sfg_seam_pending IS
    'Phase 9 SFG-seam work-queue + post-mint audit: one row per Create-WIP '
    'bom_process_route step that still lacks an SFG seam (output_code NULL or '
    'blank). The opt-in minting script targets exactly these rows; the view '
    'drains to EMPTY once every transform chain carries its SFG#### output_code, '
    'so it is the live "all chains seamed?" check. Derived, not stored.';

COMMIT;
