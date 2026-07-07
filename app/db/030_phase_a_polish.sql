-- =========================================================================
-- Migration 030: Phase A polish - manual review follow-ups
--
-- Lands the cheap, high-value DDL fixes from the Phase A code review:
--   H1.a status='closed' must have closed_at
--   H1.b terminal status (closed/cancelled) must have ended_at
--   M3   variance_qty + variance_pct as GENERATED ALWAYS columns
--        (eliminates app/persisted drift on cost-dashboard inputs)
--   M4   payload_jsonb canonical shape documentation
--   M6   closed_by sentinel documentation
--   L3   pm_variance_breakdown canonical shape pin
--   L4   last_updated_at convention divergence documentation
--
-- Skipped here (not DDL):
--   H2  Backfill script JOIN pinning - lives in scripts/029_backfill_phases.py
--   M2  Amendment status-transition trigger - app responsibility (B11 spec)
--   M1, M5  Already self-healed (named constraint) or past behavior
--
-- Idempotent. Safe to re-run.
-- =========================================================================

BEGIN;

-- H1.a: status='closed' must have closed_at
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_jcphase_closed_at_when_closed'
    ) THEN
        ALTER TABLE job_card_phase_v2
            ADD CONSTRAINT chk_jcphase_closed_at_when_closed
            CHECK (status <> 'closed' OR closed_at IS NOT NULL);
    END IF;
END $$;

-- H1.b: terminal status (closed/cancelled) must have ended_at
--       Repair backfilled rows where jc.end_time was NULL before adding
--       the constraint so it does not fire on existing data.
UPDATE job_card_phase_v2
SET    ended_at = COALESCE(closed_at, started_at)
WHERE  status IN ('closed', 'cancelled')
  AND  ended_at IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_jcphase_ended_at_when_terminal'
    ) THEN
        ALTER TABLE job_card_phase_v2
            ADD CONSTRAINT chk_jcphase_ended_at_when_terminal
            CHECK (status NOT IN ('closed', 'cancelled') OR ended_at IS NOT NULL);
    END IF;
END $$;

-- M3: variance_qty and variance_pct as GENERATED ALWAYS columns
--     Safe to DROP because B12 (variance recording) has not shipped yet;
--     the table is empty. GENERATED expressions reconstruct any value
--     identically if data ever needs to be migrated.
ALTER TABLE job_card_consumption_variance_v2 DROP COLUMN IF EXISTS variance_qty;
ALTER TABLE job_card_consumption_variance_v2 DROP COLUMN IF EXISTS variance_pct;

ALTER TABLE job_card_consumption_variance_v2
    ADD COLUMN variance_qty NUMERIC(15,3) GENERATED ALWAYS AS
        (actual_consumed_qty - bom_prescribed_qty) STORED;

ALTER TABLE job_card_consumption_variance_v2
    ADD COLUMN variance_pct NUMERIC(8,4) GENERATED ALWAYS AS
        (CASE WHEN bom_prescribed_qty = 0 THEN NULL
              ELSE ((actual_consumed_qty - bom_prescribed_qty) / bom_prescribed_qty) * 100
         END) STORED;

-- M4: payload_jsonb canonical shape per request_type
COMMENT ON COLUMN bom_amendment_request_v2.payload_jsonb IS
$DOC$Canonical payload shape per request_type:
  one_off_material_add      {"material_sku_name": text, "qty_kg": numeric, "uom": text, "reason"?: text}
  one_off_material_remove   {"material_sku_name": text, "reason"?: text}
  permanent_bom_add         {"material_sku_name": text, "qty_per_unit": numeric, "uom": text, "loss_pct"?: numeric, "item_type": "rm"|"pm"}
  permanent_bom_remove      {"bom_line_id": int}
  permanent_bom_qty_change  {"bom_line_id": int, "old_qty": numeric, "new_qty": numeric}
  plan_delete               {"plan_id": int}
  stop_process              {"reason": text}
  force_unlock              {"reason": text}
  uom_correction            {"sku_id": int, "old_uom_kg": numeric, "new_uom_kg": numeric}
  ncr_disposition           {"output_id": int, "disposition": "scrap"|"rework"|"accept_concession", "reason": text}
  unbalanced_close_override {"balance_diff_kg": numeric, "reason": text}
  ega_override              {"material_sku_name": text, "ega_kg": numeric, "reason": text}
Validated app-side via Pydantic; DB enforces request_type only.$DOC$;

-- M6: closed_by sentinel documentation
COMMENT ON COLUMN job_card_phase_v2.closed_by IS
    'User who closed the phase. Backfill rows from scripts/029_backfill_phases.py carry the sentinel "migration_029_backfill" so they can be filtered from real phase closes - used by the H2 fix in the backfill UPDATE JOINs.';

-- L3: pm_variance_breakdown canonical shape pin
COMMENT ON COLUMN job_card_accounting_v2.pm_variance_breakdown IS
$DOC$R11 PM variance roll-up. Canonical shape:
  {"pm_torn": <pcs>, "pm_damaged": <pcs>, "pm_misprint": <pcs>, "pm_rejection": <pcs>, "pm_wasted": <pcs>}
Each key optional; missing key implies 0 pcs in that bucket. JSONB so future PM categories can be added without a migration.$DOC$;

-- L4: last_updated_at convention divergence
COMMENT ON COLUMN job_card_consumption_variance_v2.last_updated_at IS
    'Touched by fn_touch_jcvar_last_updated trigger on every UPDATE. Intentionally named last_updated_at (not the v2 convention "updated_at") to make intent explicit on an upsert-heavy table - operators re-save consumption frequently and the column literally captures "last save timestamp", not the more generic "updated_at" snapshot.';

COMMIT;
