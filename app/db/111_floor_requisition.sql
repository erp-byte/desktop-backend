-- 111_floor_requisition.sql — floor requisitions: the floor asks store for a BOM
-- article it is short of, store issues it, the floor confirms receipt.
--
-- Spec: docs/superpowers/specs/2026-09-15-floor-requisitions-design.md
--
-- TRACKING ONLY. Nothing here writes new_stock_entries or stocktake_transactions;
-- floor stock still changes only through stock take.
--
-- requisition_id IS the requisition number: an app-supplied 8-digit time id
-- (app.core.helpers.new_short_time_id) inserted through insert_with_pk_retry.
-- It must be the PRIMARY KEY — that retry only catches collisions on a *_pkey
-- constraint. The numbers wrap about every 28 hours and are not in date order, so
-- every list orders by raised_at.
--
-- Every quantity is stored with its unit, and every unit on a row is the
-- requested one (kg or pcs).
--
-- Idempotent: IF NOT EXISTS throughout; the catalog insert uses the NULL-safe
-- NOT EXISTS guard from 084 (auth_permission's UNIQUE treats NULL
-- sub_sub_module as distinct, so ON CONFLICT alone would duplicate rows).

CREATE TABLE IF NOT EXISTS floor_requisition (
    requisition_id     BIGINT PRIMARY KEY,
    job_card_id        BIGINT NOT NULL REFERENCES job_card_v2(job_card_id) ON DELETE RESTRICT,
    warehouse          TEXT   NOT NULL,
    floor              TEXT   NOT NULL,
    material_sku_name  TEXT   NOT NULL,
    item_type          TEXT,

    requested_qty      NUMERIC(14,3) NOT NULL CHECK (requested_qty > 0),
    requested_unit     TEXT          NOT NULL CHECK (requested_unit IN ('kg','pcs')),

    required_qty       NUMERIC(14,3),
    required_unit      TEXT,
    available_qty      NUMERIC(14,3) NOT NULL,
    available_unit     TEXT          NOT NULL,
    shortage_qty       NUMERIC(14,3),
    shortage_unit      TEXT,

    issued_qty         NUMERIC(14,3) CHECK (issued_qty > 0),
    issued_unit        TEXT,

    status             TEXT NOT NULL DEFAULT 'raised'
                       CHECK (status IN ('raised','issued','received','cancelled')),
    note               TEXT,
    issue_note         TEXT,
    cancel_reason      TEXT,

    raised_by          TEXT NOT NULL,
    raised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    issued_by          TEXT,
    issued_at          TIMESTAMPTZ,
    received_by        TEXT,
    received_at        TIMESTAMPTZ,
    cancelled_by       TEXT,
    cancelled_at       TIMESTAMPTZ,

    CHECK (available_unit = requested_unit),
    CHECK (required_unit IS NULL OR required_unit = requested_unit),
    CHECK (shortage_unit IS NULL OR shortage_unit = requested_unit),
    CHECK (issued_unit   IS NULL OR issued_unit   = requested_unit),
    CHECK ((required_qty IS NULL) = (required_unit IS NULL)),
    CHECK ((shortage_qty IS NULL) = (shortage_unit IS NULL)),
    CHECK ((issued_qty   IS NULL) = (issued_unit   IS NULL)),
    CHECK (status NOT IN ('issued','received') OR (issued_qty IS NOT NULL AND issued_by IS NOT NULL)),
    CHECK (status <> 'received'  OR received_by IS NOT NULL),
    CHECK (status <> 'cancelled' OR (cancelled_by IS NOT NULL AND BTRIM(COALESCE(cancel_reason, '')) <> ''))
);

-- One open request per job card + article.
CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_open
    ON floor_requisition (job_card_id, UPPER(BTRIM(material_sku_name)))
    WHERE status = 'raised';

CREATE INDEX IF NOT EXISTS idx_floor_requisition_place
    ON floor_requisition (warehouse, floor, status, raised_at DESC);

CREATE INDEX IF NOT EXISTS idx_floor_requisition_job_card
    ON floor_requisition (job_card_id, raised_at DESC);

-- ── Permission catalog ──────────────────────────────────────────────────────
INSERT INTO auth_permission (module, sub_module, sub_sub_module, action, description)
SELECT v.module, v.sub_module, NULL, v.action, v.description
  FROM (VALUES
      ('production', 'floor_requisitions', 'view',    'View floor requisitions'),
      ('production', 'floor_requisitions', 'create',  'Raise a floor requisition from a job card'),
      ('production', 'floor_requisitions', 'issue',   'Issue material against a floor requisition'),
      ('production', 'floor_requisitions', 'receive', 'Confirm a floor requisition was received'),
      ('production', 'floor_requisitions', 'cancel',  'Cancel a raised floor requisition')
  ) AS v(module, sub_module, action, description)
 WHERE NOT EXISTS (
     SELECT 1
       FROM auth_permission p
      WHERE p.module         = v.module
        AND p.sub_module     IS NOT DISTINCT FROM v.sub_module
        AND p.sub_sub_module IS NULL
        AND p.action         = v.action
 );

-- ── Grants ──────────────────────────────────────────────────────────────────
-- floor_manager raises and receives; store_head issues; both may cancel a
-- request still waiting (floor: raised by mistake; store: cannot supply).
INSERT INTO auth_role_permission (role_id, permission_id)
SELECT r.role_id, p.permission_id
  FROM (VALUES
      ('admin', 'view'), ('admin', 'create'), ('admin', 'issue'), ('admin', 'receive'), ('admin', 'cancel'),
      ('floor_manager', 'view'), ('floor_manager', 'create'), ('floor_manager', 'receive'), ('floor_manager', 'cancel'),
      ('store_head', 'view'), ('store_head', 'issue'), ('store_head', 'cancel')
  ) AS g(role_name, action)
  JOIN auth_role r       ON r.role_name = g.role_name
  JOIN auth_permission p ON p.module = 'production'
                        AND p.sub_module = 'floor_requisitions'
                        AND p.sub_sub_module IS NULL
                        AND p.action = g.action
ON CONFLICT DO NOTHING;
