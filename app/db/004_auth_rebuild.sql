-- =========================================================================
-- 004_auth_rebuild.sql
-- Aligns the auth schema with the documented end-user auth API:
--   • JWT access + refresh tokens (refresh tokens stored here for rotation
--     chain tracking + reuse detection + per-session listing/revocation)
--   • Account lockout (failed_login_count, locked_until)
--   • Account status (active | suspended | disabled)
--   • Forced password change (must_change_password, password_changed_at)
--   • Per-session device_info for the /sessions UI
--
-- Idempotent. Safe to re-run. Does NOT drop the legacy auth_session table —
-- once nothing reads from it we can drop it in a separate migration.
--
-- ── REQUIREMENTS ─────────────────────────────────────────────────────────
--
-- PostgreSQL ≥ 11 required. The `ADD COLUMN ... NOT NULL DEFAULT <constant>`
-- pattern below is an instant catalog-only update on PG 11+ (defaults
-- materialise lazily). On PG ≤ 10 every ALTER below forces a full table
-- rewrite and this migration will lock auth_user for minutes on a large
-- table — split the ALTERs and run with `SET lock_timeout` if you must.
--
-- ── COST NOTES (worth thinking about for >100k user rows) ────────────────
--
--   • The ADD COLUMNs themselves: instant on PG 11+.
--   • The UPDATE ... password_changed_at backfill below: full table scan
--     plus row-by-row write. On a 100k+ table run it in batches of 10k or
--     drop the WHERE filter and accept the long-running UPDATE.
--   • The CHECK constraint: forces a full table scan to validate against
--     existing rows. Fast but not free.
-- =========================================================================

-- ── auth_user: add lockout / status / password-change columns ─────────────

ALTER TABLE auth_user
    ADD COLUMN IF NOT EXISTS status                  TEXT        NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS failed_login_count      INT         NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_until            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS must_change_password    BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS password_changed_at     TIMESTAMPTZ;

ALTER TABLE auth_user
    DROP CONSTRAINT IF EXISTS auth_user_status_check;

ALTER TABLE auth_user
    ADD CONSTRAINT auth_user_status_check
    CHECK (status IN ('active', 'suspended', 'disabled'));

-- Backfill password_changed_at for existing rows so /me has a value
UPDATE auth_user
SET    password_changed_at = COALESCE(password_changed_at, created_at)
WHERE  password_changed_at IS NULL;

-- ── auth_refresh_token: canonical refresh-token store ────────────────────
--
-- One row per refresh token ever issued. On rotation, the old row's
-- rotated_at is set and a new row is inserted whose parent_jti points
-- back. chain_root is the original login-issued jti — used for chain-wide
-- revocation when reuse is detected.

CREATE TABLE IF NOT EXISTS auth_refresh_token (
    jti              UUID         PRIMARY KEY,
    user_id          INT          NOT NULL REFERENCES auth_user(user_id) ON DELETE CASCADE,
    parent_jti       UUID,        -- previous refresh that minted this one (null for login-issued)
    chain_root       UUID         NOT NULL,
    issued_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ  NOT NULL,
    rotated_at       TIMESTAMPTZ,
    revoked_at       TIMESTAMPTZ,
    revoke_reason    TEXT,        -- 'logout' | 'logout_all' | 'reuse' | 'password_change' | 'admin'
    ip               TEXT,
    user_agent       TEXT,
    device_info      JSONB
);

CREATE INDEX IF NOT EXISTS idx_refresh_user        ON auth_refresh_token(user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_chain_root  ON auth_refresh_token(chain_root);
CREATE INDEX IF NOT EXISTS idx_refresh_active      ON auth_refresh_token(user_id)
    WHERE revoked_at IS NULL AND rotated_at IS NULL;

-- ── housekeeping helpers ─────────────────────────────────────────────────
--
-- A "session" in the /sessions UI = one *active* refresh token row
-- (revoked_at IS NULL AND rotated_at IS NULL AND expires_at > NOW()).

CREATE OR REPLACE VIEW auth_active_sessions AS
SELECT  jti AS token_id,
        user_id,
        issued_at,
        expires_at,
        ip,
        user_agent,
        device_info,
        chain_root
FROM    auth_refresh_token
WHERE   revoked_at IS NULL
  AND   rotated_at IS NULL
  AND   expires_at > NOW();
