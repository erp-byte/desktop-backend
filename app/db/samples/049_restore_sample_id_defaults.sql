-- 049_restore_sample_id_defaults.sql  (repair)
-- The original sample tables (036/037) lost their SERIAL `id` defaults in some
-- environments (the backing id sequences were dropped), so INSERTs that rely on
-- the default — requisition / article / approval / draft / gate-pass creation —
-- fail with a NOT NULL violation on id. This restores an auto-increment default
-- on every sample id column that is missing one, seeding the sequence past the
-- current MAX(id). Fully idempotent: only acts where column_default IS NULL, so
-- it is a no-op on a healthy (or freshly-migrated) database.

DO $$
DECLARE
    t        text;
    seqname  text;
    maxid    bigint;
    tables   text[] := ARRAY[
        'sample_requisitions', 'sample_requisition_articles', 'sample_approvals',
        'npd_draft_boms', 'npd_draft_bom_lines', 'sample_audit_log',
        'sample_approval_role_map', 'gate_passes', 'gate_pass_sample_details'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = t AND column_name = 'id' AND column_default IS NULL
        ) THEN
            seqname := t || '_id_seq';
            EXECUTE format('CREATE SEQUENCE IF NOT EXISTS %I', seqname);
            EXECUTE format('SELECT COALESCE(MAX(id), 0) + 1 FROM %I', t) INTO maxid;
            PERFORM setval(seqname, maxid, false);
            EXECUTE format('ALTER TABLE %I ALTER COLUMN id SET DEFAULT nextval(%L)', t, seqname);
            EXECUTE format('ALTER SEQUENCE %I OWNED BY %I.id', seqname, t);
        END IF;
    END LOOP;
END $$;
