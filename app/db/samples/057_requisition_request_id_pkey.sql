-- 057_requisition_request_id_pkey — make request_id the PRIMARY KEY of
-- sample_requisitions (was the SERIAL `id`).
--
-- `id` is NOT dropped: 10 foreign keys (incl. job_card, material_document)
-- reference sample_requisitions(id). We keep `id` as a UNIQUE auto-increment
-- column so those FKs (and all existing joins/services that resolve
-- request_id -> id) keep working unchanged. Only the PRIMARY KEY designation
-- moves to request_id, which becomes the canonical surfaced identifier.
--
-- Postgres won't drop a PK that FKs depend on, so the steps are:
--   1. request_id -> NOT NULL  (verified 0 nulls on live)
--   2. add UNIQUE(id)          (preserve FK target after PK moves)
--   3. drop the 10 inbound FKs  (they currently depend on the old PK index)
--   4. drop old PK on id, add new PK on request_id
--   5. recreate the 10 FKs referencing (id) — now backed by uq_sample_req_id
--
-- Idempotent + transactional: re-running after success is a no-op.

DO $$
DECLARE
  pk_col text;
BEGIN
  SELECT a.attname INTO pk_col
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
   WHERE t.relname = 'sample_requisitions' AND c.contype = 'p'
   LIMIT 1;

  IF pk_col = 'request_id' THEN
    RAISE NOTICE '057: request_id is already the primary key — skipping.';
    RETURN;
  END IF;

  -- 1. enforce request_id NOT NULL (0 nulls verified before writing this)
  ALTER TABLE sample_requisitions ALTER COLUMN request_id SET NOT NULL;

  -- 2. keep id unique so the inbound FKs stay valid once the PK moves off it
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_sample_req_id') THEN
    ALTER TABLE sample_requisitions ADD CONSTRAINT uq_sample_req_id UNIQUE (id);
  END IF;

  -- 3. drop the 10 inbound FKs (they depend on the old PK index)
  ALTER TABLE gate_pass_sample_details   DROP CONSTRAINT IF EXISTS gate_pass_sample_details_original_requisition_id_fkey;
  ALTER TABLE gate_pass_sample_details   DROP CONSTRAINT IF EXISTS gate_pass_sample_details_requisition_id_fkey;
  ALTER TABLE job_card                   DROP CONSTRAINT IF EXISTS job_card_sample_requisition_id_fkey;
  ALTER TABLE material_document          DROP CONSTRAINT IF EXISTS material_document_sample_requisition_id_fkey;
  ALTER TABLE npd_draft_boms             DROP CONSTRAINT IF EXISTS npd_draft_boms_requisition_id_fkey;
  ALTER TABLE rm_issue_form              DROP CONSTRAINT IF EXISTS rm_issue_form_requisition_id_fkey;
  ALTER TABLE sample_approvals           DROP CONSTRAINT IF EXISTS sample_approvals_requisition_id_fkey;
  ALTER TABLE sample_audit_log           DROP CONSTRAINT IF EXISTS sample_audit_log_requisition_id_fkey;
  ALTER TABLE sample_requisition_articles DROP CONSTRAINT IF EXISTS sample_requisition_articles_requisition_id_fkey;
  ALTER TABLE sample_requisitions        DROP CONSTRAINT IF EXISTS sample_requisitions_converted_from_id_fkey;

  -- 4. swap the primary key id -> request_id
  ALTER TABLE sample_requisitions DROP CONSTRAINT sample_requisitions_pkey;
  DROP INDEX IF EXISTS uq_sample_req_request_id;  -- free the standalone unique idx; PK builds its own
  ALTER TABLE sample_requisitions ADD CONSTRAINT sample_requisitions_pkey PRIMARY KEY (request_id);

  -- 5. recreate the 10 FKs (still referencing id, now backed by uq_sample_req_id),
  --    preserving the original ON DELETE CASCADE where present.
  ALTER TABLE gate_pass_sample_details
    ADD CONSTRAINT gate_pass_sample_details_original_requisition_id_fkey
    FOREIGN KEY (original_requisition_id) REFERENCES sample_requisitions(id);
  ALTER TABLE gate_pass_sample_details
    ADD CONSTRAINT gate_pass_sample_details_requisition_id_fkey
    FOREIGN KEY (requisition_id) REFERENCES sample_requisitions(id);
  ALTER TABLE job_card
    ADD CONSTRAINT job_card_sample_requisition_id_fkey
    FOREIGN KEY (sample_requisition_id) REFERENCES sample_requisitions(id);
  ALTER TABLE material_document
    ADD CONSTRAINT material_document_sample_requisition_id_fkey
    FOREIGN KEY (sample_requisition_id) REFERENCES sample_requisitions(id);
  ALTER TABLE npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_requisition_id_fkey
    FOREIGN KEY (requisition_id) REFERENCES sample_requisitions(id) ON DELETE CASCADE;
  ALTER TABLE rm_issue_form
    ADD CONSTRAINT rm_issue_form_requisition_id_fkey
    FOREIGN KEY (requisition_id) REFERENCES sample_requisitions(id);
  ALTER TABLE sample_approvals
    ADD CONSTRAINT sample_approvals_requisition_id_fkey
    FOREIGN KEY (requisition_id) REFERENCES sample_requisitions(id) ON DELETE CASCADE;
  ALTER TABLE sample_audit_log
    ADD CONSTRAINT sample_audit_log_requisition_id_fkey
    FOREIGN KEY (requisition_id) REFERENCES sample_requisitions(id) ON DELETE CASCADE;
  ALTER TABLE sample_requisition_articles
    ADD CONSTRAINT sample_requisition_articles_requisition_id_fkey
    FOREIGN KEY (requisition_id) REFERENCES sample_requisitions(id) ON DELETE CASCADE;
  ALTER TABLE sample_requisitions
    ADD CONSTRAINT sample_requisitions_converted_from_id_fkey
    FOREIGN KEY (converted_from_id) REFERENCES sample_requisitions(id);

  RAISE NOTICE '057: primary key moved id -> request_id; id retained as UNIQUE.';
END $$;
