-- 073: SFG boxes start life PENDING (label not yet printed). The per-box print
-- action flips them to PRINTED and persists their weighed data. Extend the
-- sfg_box status domain to allow 'PENDING' (was: PRINTED/DISPATCHED/RECEIVED/
-- CONSUMED/CANCELLED, added in 067). Idempotent: drop + re-add the CHECK.
ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_sfg_box_status;
ALTER TABLE sfg_box ADD  CONSTRAINT chk_sfg_box_status
    CHECK (status IN ('PENDING','PRINTED','DISPATCHED','RECEIVED','CONSUMED','CANCELLED'));
