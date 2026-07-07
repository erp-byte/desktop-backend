-- 044_npd_authorized_users.sql  (NPD plan §2 — the "2 specific people")
-- A data-driven allow-list that narrows the broad `npd_team` role to named users
-- per capability (AUTHOR = create/edit/start a recipe; CLOSE = close a job card;
-- PROMOTE = promote a draft to a live BOM). EMPTY for a capability = "not yet
-- configured": the role gate on the route is then sufficient and the membership
-- check is a no-op (see services/npd_auth.py). So the "given later" requirement
-- is a single INSERT, not a deploy — and every action still lands in
-- sample_audit_log with the actor.

CREATE TABLE IF NOT EXISTS npd_authorized_users (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES auth_user(user_id),
    capability  TEXT NOT NULL CHECK (capability IN ('AUTHOR', 'CLOSE', 'PROMOTE')),
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    granted_by  INT REFERENCES auth_user(user_id),
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, capability)
);
CREATE INDEX IF NOT EXISTS idx_npd_authorized_active ON npd_authorized_users(capability, active);

-- Seed the two designated NPD authors here when their user_ids are known, e.g.:
--   INSERT INTO npd_authorized_users (user_id, capability) VALUES
--     (<uid1>, 'AUTHOR'), (<uid1>, 'CLOSE'), (<uid1>, 'PROMOTE'),
--     (<uid2>, 'AUTHOR'), (<uid2>, 'CLOSE'), (<uid2>, 'PROMOTE')
--   ON CONFLICT (user_id, capability) DO NOTHING;
