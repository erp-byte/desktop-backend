-- 063_plan_created_by.sql
-- Plan List: persist the creating user on the v2 plan header so the Plan List
-- and the expanded detail preview can show "Created by". Today the header only
-- carries approved_by/approved_at/created_at — there is no record of WHO created
-- the plan — yet the frontend already wires detail.created_by (always empty).
--
-- production_plan_v2 is applied out-of-band (009_planning_v2.sql is NOT in
-- scripts/migrate.py SQL_FILES), so guard with to_regclass exactly like the SFG
-- 050-062 ALTERs. Volume + units stay derived (the list SUMs the line rows), so
-- no storage is added for those. Additive, idempotent, re-runnable.
DO $$
BEGIN
  IF to_regclass('public.production_plan_v2') IS NOT NULL THEN
    ALTER TABLE production_plan_v2 ADD COLUMN IF NOT EXISTS created_by TEXT;
    CREATE INDEX IF NOT EXISTS idx_plan_v2_created_by
      ON production_plan_v2 (created_by, created_at DESC);
  END IF;
END$$;
