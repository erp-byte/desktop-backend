-- =========================================================================
-- 033_password_reset_otp.sql
-- One-row-per-user OTP staging table for the WhatsApp-based password reset
-- flow that replaced the admin-only reset path.
--
-- Lifecycle:
--   send-otp     → INSERT ... ON CONFLICT (user_id) DO UPDATE — a resend
--                  overwrites the previous OTP + extends the TTL.
--   verify+reset → DELETE WHERE user_id — the row is erased the moment the
--                  password is set.
--
-- TTL is 1 minute (caller-side: expires_at = NOW() + interval '60 seconds').
-- We rely on the expires_at gate inside the verify endpoint; a sweeper job
-- isn't necessary because the volume is tiny (one row per active reset).
-- Add a partial index on expires_at so a future cleanup job stays cheap.
-- =========================================================================

CREATE TABLE IF NOT EXISTS auth_password_reset_otp (
    user_id      INT          PRIMARY KEY REFERENCES auth_user(user_id) ON DELETE CASCADE,
    otp_hash     TEXT         NOT NULL,             -- bcrypt(otp); never store raw
    expires_at   TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_phone   TEXT                              -- last WA number the OTP was sent to (audit)
);

CREATE INDEX IF NOT EXISTS idx_auth_password_reset_otp_expires_at
    ON auth_password_reset_otp(expires_at);
