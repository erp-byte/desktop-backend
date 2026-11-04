-- 133_customer_returns_permissions.sql — the permissions every Customer Returns
-- write now checks on the server (app/modules/customer_returns/permissions.py).
--
-- Until 28 Oct 2026 those endpoints checked only that the caller was logged in,
-- while the screens opened only for admins, so any account could create, edit or
-- delete a return, or write its boxes, by calling the API directly.
--
-- CATALOG ONLY: no role is granted any of these here. Admins pass on their flag,
-- exactly as before; anyone else is refused until a role is granted the action on
-- the admin screen. The backend does not need this file to be safe: without it the
-- check finds no grant and refuses, so it may deploy before or after this runs.
--
-- Idempotent: the NULL-safe NOT EXISTS guard from 084 (auth_permission's UNIQUE
-- treats a NULL sub_sub_module as distinct, so ON CONFLICT alone would duplicate).

INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, v.sub_module, NULL, v.action, v.description
  FROM (VALUES
      ('customer_returns', 'returns', 'create', 'Raise a customer return and send it for approval'),
      ('customer_returns', 'returns', 'edit',   'Edit a customer return''s header and lines while it is undecided'),
      ('customer_returns', 'returns', 'delete', 'Delete a customer return'),
      ('customer_returns', 'boxes',   'edit',   'Enter, print and remove customer-return boxes')
  ) AS v(module, sub_module, action, description)
 WHERE NOT EXISTS (
     SELECT 1
       FROM auth_permission p
      WHERE p.module         = v.module
        AND p.sub_module     IS NOT DISTINCT FROM v.sub_module
        AND p.sub_sub_module IS NULL
        AND p.action         = v.action
 );
