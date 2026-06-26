-- ===========================================================================
-- 0000_pre_objects.sql   (TEST DB — applied FIRST, before schema.sql)
-- ---------------------------------------------------------------------------
-- Out-of-band database objects that server_replica's migrations REFERENCE but
-- never CREATE (defined manually in the prod DB). They must exist before the
-- foundational schemas run, or a fresh build fails.
--
--   e_extraction_status : enum used by the SO-ingest pipeline
--       (so ingest casts 'extracted'::e_extraction_status; migrate.sql does a
--        guarded ALTER TYPE ... ADD VALUE 'failed'). Created here with the values
--        the app uses; the guarded ALTER then no-ops.
--
-- Idempotent (guarded CREATE TYPE).
-- ===========================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'e_extraction_status') THEN
        CREATE TYPE e_extraction_status AS ENUM ('pending', 'extracted', 'failed', 'error');
    END IF;
END $$;
