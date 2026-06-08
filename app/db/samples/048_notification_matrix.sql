-- 048_notification_matrix.sql  (NPD plan §11 — maker-checker mail / approval matrix)
-- Data-driven routing so every gated sample/NPD event reaches the right approver
-- role and the right team inbox WITHOUT a deploy. Each (event × sample_type) row
-- says which team(s) get an in-app alert + email, and whether the gate is
-- maker-checker. Notifications fan out via services/sample_notify_service.py,
-- which writes store_alert (in-app) and sends SMTP email (reusing the existing
-- mail_service transport — no-op until SMTP_* is configured in the env).

CREATE TABLE IF NOT EXISTS sample_notification_map (
    id                     SERIAL PRIMARY KEY,
    event                  TEXT NOT NULL,            -- RM_FORM_RAISED, RM_FORM_ISSUED, BH_APPROVAL, ...
    sample_type            TEXT NOT NULL DEFAULT '*',
    notify_teams           TEXT[] NOT NULL DEFAULT '{}',   -- stores/inventory/npd/business/production
    mail_template          TEXT,
    mail_enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    requires_maker_checker BOOLEAN NOT NULL DEFAULT FALSE,
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event, sample_type)
);

INSERT INTO sample_notification_map (event, sample_type, notify_teams, requires_maker_checker) VALUES
    ('RM_FORM_RAISED', '*', ARRAY['stores', 'inventory'], TRUE),
    ('RM_FORM_ISSUED', '*', ARRAY['npd'],                 TRUE),
    ('BH_APPROVAL',    '*', ARRAY['business', 'npd'],      TRUE),
    ('INV_SIGNOFF',    '*', ARRAY['inventory', 'business'],TRUE),
    ('DISPATCH',       '*', ARRAY['inventory', 'business'],TRUE)
ON CONFLICT (event, sample_type) DO NOTHING;
