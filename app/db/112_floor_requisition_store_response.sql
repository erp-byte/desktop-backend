-- 112_floor_requisition_store_response.sql — store's WhatsApp reply to a raised
-- floor requisition.
--
-- When a request is raised, store_head users get the approved WhatsApp template
-- floor_requisition_raised_store, whose quick replies are "Accept" ("take it up")
-- and "Hold" ("cannot be supplied right now"). The tap is recorded here, on the
-- request itself, so the floor sees it on the job card and store sees it in
-- Stores → Production Indents.
--
-- It is NOT a status: the request stays 'raised' until store issues it. The three
-- columns hold the LATEST reply (a Hold can later become an Accept), who gave it
-- and when. They are only written while the request is 'raised'.
--
-- The application reads these columns only after checking they exist, so the
-- backend may deploy before or after this runs. Idempotent.

ALTER TABLE floor_requisition ADD COLUMN IF NOT EXISTS store_response    TEXT;
ALTER TABLE floor_requisition ADD COLUMN IF NOT EXISTS store_response_by TEXT;
ALTER TABLE floor_requisition ADD COLUMN IF NOT EXISTS store_response_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'floor_requisition'::regclass
           AND conname  = 'ck_floor_requisition_store_response'
    ) THEN
        ALTER TABLE floor_requisition
            ADD CONSTRAINT ck_floor_requisition_store_response CHECK (
                (store_response IS NULL AND store_response_by IS NULL AND store_response_at IS NULL)
             OR (store_response IN ('accepted', 'on_hold')
                 AND store_response_by IS NOT NULL AND store_response_at IS NOT NULL)
            );
    END IF;
END
$$;
