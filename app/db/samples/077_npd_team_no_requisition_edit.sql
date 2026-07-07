-- 077_npd_team_no_requisition_edit.sql
-- Matrix: NPD team has all access on NPD requests EXCEPT edit and cancel.
-- The requisition PATCH and cancel routes both gate on ('sample','requisition','edit').
-- 035 granted npd_team that permission; revoke it so edit/cancel are blocked at the
-- API too (npd_team keeps 'requisition/create' for raising + submitting, and the
-- 'npd' permissions for dev-job-card authoring/promotion).
--
-- Side effect (intended, per the approved matrix): an NPD user can no longer edit or
-- cancel a requisition — including their own draft — via the API; they create + submit
-- only. The frontend already hides edit/cancel for npd_team; this aligns the backend.
--
-- Uses IN (not =) for both subqueries: auth_permission's unique constraint treats
-- a NULL sub_sub_module as distinct, so re-runs of 035 can leave duplicate
-- (sample, requisition, NULL, edit) rows. IN handles >1 match; = would raise
-- "more than one row returned by a subquery" (21000).
--
-- Idempotent. Safe to re-run.

DELETE FROM auth_role_permission
WHERE role_id IN (SELECT role_id FROM auth_role WHERE role_name = 'npd_team')
  AND permission_id IN (
    SELECT permission_id FROM auth_permission
    WHERE module = 'sample' AND sub_module = 'requisition'
      AND sub_sub_module IS NULL AND action = 'edit');
