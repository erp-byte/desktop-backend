-- 086_cr_deputy_approver.sql
-- Customer Returns: R M Patil as a standing CC, and a SECOND approver standing
-- behind the CR's Business Head.
--
-- WHY A DEPUTY EXISTS
--   A customer return has to be closed the day it is raised. One named BU Head is
--   a single point of failure — travel, leave, a phone left on charge — and the
--   return then sits Pending overnight. Satyendra Garg and R M Patil stand in.
--
--   They are notified on EVERY return, not only once someone declares the primary
--   unavailable: there is no availability signal in the system, and a deputy told
--   after the fact cannot save the same day. Two people holding the buttons is
--   safe because the decision is idempotent (approval_service.apply_cr_action) —
--   the first Approve wins and the second is reported as already actioned, so a
--   simultaneous tap cannot produce two different outcomes.
--
-- WHY match_key CARRIES A NAME HERE
--   The other kinds in this table need no name. A deputy does: the WhatsApp leg
--   resolves a phone from auth_user by full_name, exactly as the primary BU Head
--   is resolved. The name must match auth_user.full_name EXACTLY — when it does
--   not, the mail still goes and only the WhatsApp is skipped (logged, never
--   fatal). 'R M Patil' has no auth_user row today, so he is mail-only until one
--   exists with his number.
--
-- Additive and idempotent; no schema change (kind is free text).

INSERT INTO cr_email_routing (kind, match_key, match_type, email, sort_order) VALUES
    -- Mandatory CC on every customer-return mail.
    ('constant_cc', '', 'const', 'rmpatil@candorfoods.in', 8),

    -- Standing deputy approvers — mail buttons + WhatsApp, same as the BU Head.
    ('deputy_approver', 'Satyendra Kumar Garg', 'name', 'satyendra@candorfoods.in', 1),
    ('deputy_approver', 'R M Patil',            'name', 'rmpatil@candorfoods.in',   2)
ON CONFLICT (kind, match_key, email) DO NOTHING;
