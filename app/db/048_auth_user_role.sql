-- 048_auth_user_role.sql — multiple roles per user
--
-- A person can hold more than one role at once (e.g. admin + business_head).
-- This join table is the source of truth for a user's FULL role set; the
-- existing auth_user.role_id stays as the "primary"/display role (the first
-- one selected) so every legacy read that joins on auth_user.role_id keeps
-- resolving a sensible single role.
--
-- Effective behaviour (enforced in the services, not the DB):
--   * is_admin            = ANY assigned role is_admin
--   * permission granted  = ANY assigned role grants it
-- Resolution falls back to [auth_user.role_id] when a user has no rows here,
-- so accounts created before this migration keep working without a backfill —
-- but we backfill anyway so the join is authoritative going forward.

CREATE TABLE IF NOT EXISTS auth_user_role (
    user_id  INT NOT NULL REFERENCES auth_user(user_id) ON DELETE CASCADE,
    role_id  INT NOT NULL REFERENCES auth_role(role_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_auth_user_role_user ON auth_user_role(user_id);

-- Backfill each user's current single role so the join is authoritative.
INSERT INTO auth_user_role (user_id, role_id)
SELECT user_id, role_id
  FROM auth_user
 WHERE role_id IS NOT NULL
ON CONFLICT (user_id, role_id) DO NOTHING;
