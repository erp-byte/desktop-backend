-- 063_dev_jc_phase_recipe.sql
-- Trial recipe is now stored PER PHASE. Each phase owns its own recipe lines (a
-- fresh formulation per trial iteration); a new phase clones the previous phase's
-- recipe as a starting point. Lines with phase_id IS NULL remain the card-level
-- "base" recipe (the starting point cloned by the first phase). Closing the card
-- promotes the recipe of an operator-chosen phase. Additive + idempotent.
ALTER TABLE npd_dev_job_card_lines
    ADD COLUMN IF NOT EXISTS phase_id BIGINT
        REFERENCES npd_dev_job_card_phases(phase_id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_npd_dev_jc_lines_phase ON npd_dev_job_card_lines(phase_id);
