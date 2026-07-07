-- =========================================================================
-- Migration 025: position roles for the operations team
--
-- The canonical positions used by the operations team are:
--   so_upload        — uploads SO files into the ingestion pipeline
--   planner          — production planner (already seeded)
--   floor_manager    — floor operations (already seeded)
--   production_head  — final sign-off authority on every closed JC
--
-- Existing seed in auth_schema.sql already has `planner` and
-- `floor_manager`. This migration adds the two missing ones and is
-- idempotent (the ON CONFLICT DO NOTHING means a re-run is a no-op).
--
-- After this, REQUIRED_SIGN_OFFS in job_card_v2.py (Python) is updated
-- to `('production_head',)` so closing a JC requires the production
-- head's sign-off (and only theirs — the floor in-charge and QC
-- inspector roles still exist for audit, but no longer gate close).
-- =========================================================================

BEGIN;

INSERT INTO auth_role (role_name, description, is_admin) VALUES
    ('so_upload',       'SO upload — ingests sales orders into the pipeline', FALSE),
    ('production_head', 'Production head — sign-off authority on JC close',   FALSE)
ON CONFLICT (role_name) DO NOTHING;

COMMIT;

-- Verification: all four operations positions should be present after this.
SELECT role_name, description, is_admin
  FROM auth_role
 WHERE role_name IN ('so_upload', 'planner', 'floor_manager', 'production_head')
 ORDER BY role_name;
