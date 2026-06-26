---
feature: Job Card Chain & Partial Dispatch
reviewed: 2026-04-18T00:00:00Z
depth: standard
reviewer: gsd-code-reviewer (Claude Opus 4.7)
files_reviewed: 5
files_reviewed_list:
  - app/db/001_job_card_chain.sql
  - app/db/production_schema.sql
  - app/db/production_migrate.sql
  - app/modules/production/services/job_card_engine.py
  - app/modules/production/router.py
findings:
  critical: 4
  high: 5
  medium: 5
  low: 4
  total: 18
status: issues_found
verdict: BLOCK — do not ship to production
---

# Job Card Chain & Partial Dispatch — Code Review

## Executive Summary

The new chain + partial-dispatch feature adds useful multi-stage WIP tracking, and the
happy-path smoke tests confirm the basic design works end-to-end. However, the
implementation has **four critical, production-blocking defects** clustered in three areas:

1. **The column-drop migration in `production_migrate.sql` is not self-consistent with the
   running code.** `day_end.py` still `SELECT`s `o.offgrade_kg` and `o.dispatch_qty` from
   `job_card_output`, and `bulk_dispatch` still `UPDATE`s `dispatch_qty`. After migration 11
   runs, `GET /api/v1/production/day-end/summary` and `POST .../day-end/bulk-dispatch` will
   both raise `UndefinedColumnError` (HTTP 500). The PDF generator (`job_card_pdf.py`) also
   references `rejection_kg` / `material_return_kg` — those degrade silently to `--` but
   represent lost information.

2. **`dispatch_partial_to_next_stage` has a textbook TOCTOU race.** It reads
   `dispatched_to_next_kg`, validates against `batch_size`, then increments with
   `UPDATE ... SET dispatched_to_next_kg = dispatched_to_next_kg + $1` — no `SELECT … FOR
   UPDATE`. Two simultaneous POSTs of 100 kg each against a JC with 150 kg remaining will
   both pass validation and the total dispatched will become 200 kg (exceeding batch size).
   The router's `conn.transaction()` wrapper gives atomicity but not isolation for this
   check-then-update pattern at the default READ COMMITTED isolation level.

3. **The chain-management endpoints have no authentication, authorization, or entity scoping
   on reads.** `GET /orders/{id}/job-card-chain` and `GET /job-cards/{id}/dispatch-log` return
   data for any production order regardless of who is asking or which entity (`cfpl`/`cdpl`)
   they belong to. `POST /job-cards/{id}/dispatch-to-next` takes `dispatched_by` as a plain
   string in the request body — the caller can impersonate anyone. The same production
   router module has *no* `Depends(...)` references anywhere, confirming this is a
   module-wide gap, not a chain-specific one; but the chain feature extends the gap.

4. **No cycle detection or entity/order-consistency check when chaining job cards.** The
   `next_job_card_id`/`prev_job_card_id` columns have no DB-level constraint that prevents
   (a) a JC from pointing to itself, (b) a cycle A→B→A, (c) a JC in order X pointing to a
   JC in order Y, or (d) JCs from different entities being chained. `create_job_cards`
   builds a safe chain today, but any future code path that touches these columns
   (force-unlock flows, manual re-chaining, data fixes) can corrupt the graph without
   noise.

Beyond the criticals, there are five high-severity issues around status-state-machine
correctness (dispatch accepted into `completed`/`cancelled` downstream JCs, dispatch
allowed from a JC whose own output has never been recorded, floor-indent status set to
`'draft'` which isn't in the documented lifecycle, etc.), five medium issues
(non-transactional sibling concerns in the router, missing indexes, FK `ON DELETE` default,
`COUNT(*)+1` indent-number race), and four low-severity nits.

**Verdict: BLOCK — do not ship to production.** The four critical items are either silent
data-loss risks (day-end summary stops working, over-dispatch possible) or security gaps
(no auth, no entity scoping). At minimum, fix day-end, add row-level locking to dispatch,
add one DB trigger/CHECK for cycle prevention, and make a decision (with documentation)
about the auth model before exposing this to the frontend.

---

## Critical (4)

### CR-01 — `day_end.py` queries dropped columns (`offgrade_kg`, `dispatch_qty`); day-end API will 500 after migration

- **File:** `app/modules/production/services/day_end.py:25-41`, `:62-72`
- **Category:** data-loss / bug
- **Description:** `production_migrate.sql` migration step 11 (lines 513–523) drops
  `offgrade_kg` and `dispatch_qty` from `job_card_output`. But `get_day_end_summary`
  still `SELECT`s `o.offgrade_kg, o.dispatch_qty`, and `bulk_dispatch` still does
  `UPDATE job_card_output SET dispatch_qty = $2`. The migration comment on line 479
  claims data was moved manually, but no code search was done to update consumers.
- **Failure mode:** Any caller of `GET /api/v1/production/day-end/summary` gets a 500
  with `asyncpg.exceptions.UndefinedColumnError: column o.offgrade_kg does not exist`.
  `POST /api/v1/production/day-end/bulk-dispatch` likewise fails at the UPDATE.
  Operational dispatch cannot complete.
- **Fix:**
  ```python
  # day_end.py — get_day_end_summary
  rows = await conn.fetch(
      """
      SELECT jc.job_card_id, jc.job_card_number, jc.fg_sku_name, jc.customer_name,
             jc.batch_number, jc.batch_size_kg, jc.step_number, jc.status,
             o.fg_expected_units, o.fg_actual_units, o.fg_expected_kg, o.fg_actual_kg,
             o.process_loss_kg, o.net_output_kg, o.yield_pct,
             COALESCE(bp.total_bp_kg, 0) AS offgrade_kg,
             jc.dispatched_to_next_kg AS dispatch_qty   -- or SUM from partial_dispatch
      FROM job_card jc
      JOIN production_order po ON jc.prod_order_id = po.prod_order_id
      LEFT JOIN job_card_output o ON jc.job_card_id = o.job_card_id
      LEFT JOIN (
          SELECT job_card_id, SUM(quantity_kg) AS total_bp_kg
          FROM job_card_byproduct GROUP BY job_card_id
      ) bp ON bp.job_card_id = jc.job_card_id
      WHERE jc.entity = $1
        AND jc.step_number = po.total_stages
        AND jc.status IN ('completed', 'closed')
        AND DATE(jc.end_time) = $2
      ORDER BY jc.end_time
      """,
      entity, d,
  )
  ```
  And redesign `bulk_dispatch`: since `dispatch_qty` is gone, either (a) add a new
  `dispatched_qty_kg` column to `job_card_output`, (b) route bulk dispatch through the
  `job_card_partial_dispatch` table, or (c) delete the endpoint if it's superseded by
  the new per-JC dispatch-to-next flow. Pick one and document it. Also update
  `mcp_tracker.py:341-343` which has the same query.

### CR-02 — Race condition in `dispatch_partial_to_next_stage` can over-dispatch past `batch_size_kg`

- **File:** `app/modules/production/services/job_card_engine.py:1015-1039`
- **Category:** concurrency / data-integrity
- **Description:** The function does
  ```python
  jc = await conn.fetchrow("SELECT * FROM job_card WHERE job_card_id = $1", ...)
  already = float(jc['dispatched_to_next_kg'] or 0)
  remaining = float(jc['batch_size_kg']) - already
  if qty_kg > remaining: return {"error": ...}
  await conn.execute("UPDATE job_card SET dispatched_to_next_kg = dispatched_to_next_kg + $1 ...")
  ```
  There is no `FOR UPDATE` on the `SELECT`, and PostgreSQL's default isolation is READ
  COMMITTED. Two concurrent requests each read `dispatched_to_next_kg = 0`, each compute
  `remaining = 1000`, both pass validation with `qty=600`, and both UPDATEs commit
  (additive, because the UPDATE clause itself uses `dispatched_to_next_kg + $1`). Final
  value: 1200 kg dispatched when batch is 1000 kg.
- **Failure mode:** Silent breach of the `dispatched_to_next_kg ≤ batch_size_kg` invariant.
  Downstream JC receives more WIP than exists. Floor inventory becomes negative or
  inconsistent. No error surfaced to either caller.
- **Fix:** Either (a) lock the source row, or (b) make the validation itself atomic via
  the UPDATE's WHERE clause, or (c) add a DB CHECK constraint that UPDATE violates.
  Preferred (c) + (b):
  ```python
  # Add to 001_job_card_chain.sql (defensive DB-layer guard):
  ALTER TABLE job_card
      ADD CONSTRAINT chk_jc_dispatched_not_over_batch
      CHECK (dispatched_to_next_kg <= batch_size_kg);

  # And in dispatch_partial_to_next_stage, perform the cap atomically:
  updated = await conn.fetchval(
      """
      UPDATE job_card
         SET dispatched_to_next_kg = dispatched_to_next_kg + $1
       WHERE job_card_id = $2
         AND status = 'in_progress'
         AND next_job_card_id IS NOT NULL
         AND dispatched_to_next_kg + $1 <= batch_size_kg
       RETURNING dispatched_to_next_kg
      """,
      qty_kg, job_card_id,
  )
  if updated is None:
      # Re-fetch to build a precise error message
      jc = await conn.fetchrow("SELECT status, batch_size_kg, dispatched_to_next_kg, "
                               "next_job_card_id FROM job_card WHERE job_card_id = $1",
                               job_card_id)
      if not jc:
          return {"error": "job_card not found"}
      if jc['status'] != 'in_progress':
          return {"error": f"status is {jc['status']}, must be in_progress"}
      if not jc['next_job_card_id']:
          return {"error": "no next stage"}
      remaining = float(jc['batch_size_kg']) - float(jc['dispatched_to_next_kg'])
      return {"error": f"qty {qty_kg} exceeds remaining {remaining}"}
  ```
  The `RETURNING` makes the UPDATE fail-closed: no update, no error silently. The CHECK
  constraint adds belt-and-braces enforcement at the DB level.

### CR-03 — No authentication/authorization anywhere on the production router; chain endpoints leak cross-entity data

- **File:** `app/modules/production/router.py:788-821` (chain read), `:994-1016` (dispatch),
  `:1596-1620` (dispatch log), plus the router as a whole
- **Category:** security
- **Description:**
  - `GET /api/v1/production/orders/{prod_order_id}/job-card-chain` takes only the
    path ID and has no `entity` filter or auth dependency. Any caller that can reach the
    backend can enumerate chains across both `cfpl` and `cdpl`.
  - `GET /api/v1/production/job-cards/{id}/dispatch-log` — same.
  - `POST /api/v1/production/job-cards/{id}/dispatch-to-next` requires `entity` as a
    query parameter but does not validate that the job card belongs to that entity; a
    user who knows a job card ID can pass `entity=cdpl` while dispatching a `cfpl` JC.
    Worse, `body.dispatched_by: str` is a plain free-text field — there is no binding
    to a request principal, so the audit trail in `job_card_partial_dispatch.dispatched_by`
    is trivially forgeable.
  - Grep confirms the entire production router has **zero** `Depends(...)` references
    and `main.py:68-73` mounts only CORS middleware (with `allow_origins=["*"]`, i.e.
    wide open). There is an `app/modules/auth/middleware.py` module but it's not wired
    in.
- **Failure mode:** Tenancy breach (cfpl user sees cdpl orders), audit-log forgery
  (`dispatched_by` is attacker-controlled), and unauthenticated writes to production data.
- **Fix:**
  1. Decide and document the auth model (JWT? session? mTLS?).
  2. Add a dependency: `Depends(require_permission("production.dispatch"))` on the
     dispatch endpoint and `Depends(require_permission("production.read"))` on reads.
  3. Derive `dispatched_by` from the authenticated principal, not from the request body.
  4. In every chain query, scope by entity extracted from the principal — `WHERE
     prod_order_id = $1 AND entity = $2`.
  5. In `dispatch_partial_to_next_stage`, verify `jc['entity'] == entity` parameter;
     reject mismatch with 403.

### CR-04 — No cycle/self-reference/cross-order detection on `next_job_card_id` / `prev_job_card_id`

- **File:** `app/db/001_job_card_chain.sql:13-23`, `app/db/production_schema.sql:246-258`,
  `app/modules/production/services/job_card_engine.py:245-256`
- **Category:** data-integrity / design
- **Description:** `next_job_card_id INT REFERENCES job_card(job_card_id)` has no
  constraints beyond the FK itself. Nothing prevents:
  - `UPDATE job_card SET next_job_card_id = job_card_id WHERE job_card_id = 42;`
    (self-loop)
  - An operator/support script creating a chain A→B→A (traversals would infinite-loop).
  - Chaining JCs that belong to **different `prod_order_id`s** — the dispatch code uses
    `jc['next_job_card_id']` verbatim and doesn't re-verify same-order.
  - Chaining JCs that belong to **different entities** (`cfpl`→`cdpl` or vice versa) —
    tenancy silently breached.
  - The audit FK on `job_card_partial_dispatch.(from_job_card_id,to_job_card_id)` could
    reference JCs in unrelated orders without complaint.
  Today only `create_job_cards` writes these columns and it writes them safely, but the
  invariants are not encoded in the DB, so any future code path or manual DB surgery can
  corrupt the graph.
- **Failure mode:** A bad update loops `complete_job_card`'s "find next" traversal; a
  cross-order chain produces nonsense `GET /orders/{id}/job-card-chain` results;
  cross-entity chain leaks WIP across `cfpl`/`cdpl`.
- **Fix:** Add DB-level guards in `001_job_card_chain.sql`:
  ```sql
  -- Self-loop
  ALTER TABLE job_card
      ADD CONSTRAINT chk_jc_no_self_chain
      CHECK (next_job_card_id IS NULL OR next_job_card_id <> job_card_id);
  ALTER TABLE job_card
      ADD CONSTRAINT chk_jc_no_self_prev
      CHECK (prev_job_card_id IS NULL OR prev_job_card_id <> job_card_id);

  -- Same prod_order + same entity enforcement via trigger
  CREATE OR REPLACE FUNCTION validate_jc_chain_refs() RETURNS TRIGGER AS $$
  BEGIN
      IF NEW.next_job_card_id IS NOT NULL THEN
          PERFORM 1 FROM job_card
            WHERE job_card_id = NEW.next_job_card_id
              AND prod_order_id = NEW.prod_order_id
              AND entity IS NOT DISTINCT FROM NEW.entity;
          IF NOT FOUND THEN
              RAISE EXCEPTION 'next_job_card_id % not in same prod_order/entity as jc %',
                  NEW.next_job_card_id, NEW.job_card_id;
          END IF;
      END IF;
      -- Same for prev_job_card_id
      RETURN NEW;
  END $$ LANGUAGE plpgsql;

  CREATE TRIGGER trg_validate_jc_chain
      BEFORE INSERT OR UPDATE OF next_job_card_id, prev_job_card_id ON job_card
      FOR EACH ROW EXECUTE FUNCTION validate_jc_chain_refs();
  ```
  Cycle detection for chains longer than 2 is harder in a trigger; document that code is
  the authoritative writer and add a startup sanity script
  (`scripts/verify_jc_chain_consistency.py`) that runs a recursive CTE to flag cycles.

---

## High (5)

### HI-01 — Dispatch unlocks the next JC but does not check for terminal states

- **File:** `app/modules/production/services/job_card_engine.py:1041-1050`
- **Category:** bug / state-machine
- **Description:** The code does:
  ```python
  if next_jc['status'] == 'locked':
      # unlock
  ```
  But it **always** increments `carried_qty_kg` and logs the dispatch regardless of
  `next_jc['status']`. If the next JC is `completed`, `closed`, or `cancelled`, the
  dispatch still succeeds — WIP is pushed into a terminal JC that will never process it.
  Per the prompt this was explicitly called out as a concern; confirmed bug.
- **Fix:**
  ```python
  TERMINAL_NEXT_STATES = {'completed', 'closed', 'cancelled'}
  if next_jc['status'] in TERMINAL_NEXT_STATES:
      return {"error": f"Next stage JC is {next_jc['status']}; cannot dispatch"}
  if next_jc['status'] == 'locked':
      await conn.execute("UPDATE job_card SET status='unlocked', is_locked=FALSE, "
                         "locked_reason=NULL WHERE job_card_id = $1", next_jc_id)
  # else: already unlocked/in_progress — just carry more qty, that's fine
  ```

### HI-02 — Dispatch does not require current JC to have recorded output (`fg_actual_kg`)

- **File:** `app/modules/production/services/job_card_engine.py:1015-1030`
- **Category:** bug / business-rule
- **Description:** The validator checks `jc['status'] == 'in_progress'` and that
  `qty_kg <= batch_size_kg - dispatched_to_next_kg`, but never checks that the source JC
  has actually produced output. An operator can call dispatch-to-next with `qty_kg=900`
  on a JC where `fg_actual_kg` is still NULL (or 0), manufacturing WIP out of thin air.
  `dispatched_to_next_kg` is bounded by `batch_size_kg` (the *input* target), not by
  `fg_actual_kg` (the *produced* quantity).
- **Fix:** Cap dispatch against actual output-to-date, not batch_size:
  ```python
  output = await conn.fetchrow(
      "SELECT fg_actual_kg FROM job_card_output WHERE job_card_id = $1", job_card_id,
  )
  produced = float(output['fg_actual_kg']) if output and output['fg_actual_kg'] else 0.0
  remaining = produced - already_dispatched
  if qty_kg > remaining:
      return {"error": f"qty {qty_kg} exceeds produced-minus-dispatched ({remaining})"}
  ```
  Decide whether an operator should be able to pre-dispatch WIP before recording
  `fg_actual_kg` (likely no, for audit cleanliness) and document the choice.

### HI-03 — `check_and_raise_floor_indents` uses `status='draft'` which is outside the documented lifecycle

- **File:** `app/modules/production/services/job_card_engine.py:324-335, 361-372`
- **Category:** bug / consistency
- **Description:** `production_schema.sql:565` documents `purchase_indent.status` values
  as `'raised, acknowledged, po_created, received, cancelled'` (no CHECK constraint,
  just a comment). This code inserts new indents with `status='draft'`. Other places in
  the codebase (`mcp_server.py:805-810`, `mcp_planner.py:545-549`) also use `'draft'` as
  a pre-submission state and then UPDATE to `'raised'`, so there's precedent — but this
  new call site inserts `'draft'` and **never** promotes it to `'raised'`, so the indent
  is invisible to MRP / purchase dashboards that filter by `status IN ('raised',
  'acknowledged')` (e.g. `test_plan_generation.py:204`, `mcp_server.py:278`).
- **Failure mode:** Silent loss of floor-shortfall indents — purchase team never sees
  them.
- **Fix:** Either (a) change to `status='raised'` so MRP picks it up immediately, or
  (b) add an explicit auto-promotion step after creation. Whichever is chosen, add a
  CHECK constraint on `purchase_indent.status` enumerating all valid states, so this
  kind of drift surfaces at write time instead of silently.

### HI-04 — `check_and_raise_floor_indents` generates duplicate indent numbers under concurrency

- **File:** `app/modules/production/services/job_card_engine.py:319-323, 356-360`
- **Category:** concurrency / bug
- **Description:** `indent_number` is built from
  ```python
  seq = await conn.fetchval(
      "SELECT COUNT(*) + 1 FROM purchase_indent WHERE indent_number LIKE $1",
      f"IND-{today_str}%",
  )
  ```
  `purchase_indent.indent_number` is `TEXT NOT NULL UNIQUE`. Two concurrent JC creations
  each see the same count and produce the same number; the second INSERT fails with a
  unique-violation, aborting the transaction and leaving the job_card without its
  floor-shortfall indent. Also happens inside the same request because the code runs in
  a loop over `rm_indents` and `pm_indents`, and each iteration issues its own
  `COUNT(*)+1` — but because both are inside the same transaction the count doesn't see
  prior inserts, so the **second iteration in the same request** also gets a duplicate
  number.
- **Failure mode:** `asyncpg.exceptions.UniqueViolationError` on the second shortfall
  indent, aborting the whole `create_job_cards` transaction and leaving no JC at all.
- **Fix:** Use a sequence:
  ```sql
  CREATE SEQUENCE IF NOT EXISTS purchase_indent_daily_seq;
  ```
  ```python
  seq = await conn.fetchval("SELECT nextval('purchase_indent_daily_seq')")
  indent_number = f"IND-{today_str}-{seq:03d}"
  ```
  (Daily-reset semantics are lost, but collision is eliminated. If you really want
  daily reset, use `to_char(NOW(),'YYYYMMDD') || '-' || lpad(nextval('daily_seq')::text,3,'0')`
  with a nightly `ALTER SEQUENCE … RESTART`.)

### HI-05 — Transaction scope on dispatch is at the router, but the engine's 4 writes are OK only if caller wraps; no defensive savepoint

- **File:** `app/modules/production/router.py:1005-1013`,
  `app/modules/production/services/job_card_engine.py:1009-1084`
- **Category:** design / robustness
- **Description:** The router does:
  ```python
  async with pool.acquire() as conn:
      jc = await conn.fetchrow("SELECT job_card_number FROM job_card WHERE job_card_id = $1", job_card_id)
      async with conn.transaction():
          result = await dispatch_partial_to_next_stage(conn, ...)
  ```
  This gives atomicity for the four engine writes, but (a) the `jc = conn.fetchrow(...)`
  read before the transaction is unscoped, (b) the engine re-fetches the same JC inside
  the transaction (double round-trip), and (c) if any future code path imports
  `dispatch_partial_to_next_stage` directly (e.g. from an MCP tool, batch job, or
  webhook handler) and forgets the `async with conn.transaction()`, the four writes will
  auto-commit individually and a partial failure leaves the ledger inconsistent.
- **Fix:** Move the transaction boundary into the engine function itself using
  `async with conn.transaction():` around steps 4-7, so every caller gets atomicity
  regardless of wrapper. If the caller already has an outer transaction, asyncpg will
  create a SAVEPOINT — that's fine. Additionally, remove the unnecessary double fetch
  by returning `job_card_number` from the engine's result dict.

---

## Medium (5)

### ME-01 — `GET /orders/{prod_order_id}/job-card-chain` has no `entity` filter in the SQL

- **File:** `app/modules/production/router.py:788-821`
- **Category:** design / tenancy
- **Description:** The query filters only by `prod_order_id`. Combined with CR-03, a
  logged-in `cfpl` user (once auth is added) can still see a `cdpl` order's chain just
  by guessing the ID. Once tenant-scoping is added, include `AND entity = $2`.
- **Fix:** Pass `entity` in from the principal; add `AND jc.entity = $2` to the `WHERE`.

### ME-02 — Dropped FK `ON DELETE` behavior leaves orphans / blocks deletions unpredictably

- **File:** `app/db/001_job_card_chain.sql:14,16,30,31`
- **Category:** design
- **Description:** All four FKs (`next_job_card_id`, `prev_job_card_id`,
  `job_card_partial_dispatch.from_job_card_id`, `.to_job_card_id`) default to `ON DELETE
  NO ACTION`. Deleting a job card that is anyone's `next` or `prev` will fail silently
  (transaction abort) and so will cancellation flows that try to purge. Because no
  deletion path exists today this is latent, but will surface the first time an admin
  needs to clean up a botched order.
- **Fix:** Decide the semantic and encode it. Suggest:
  ```sql
  -- prev/next: clear the pointer on delete, keep remaining chain valid
  ALTER TABLE job_card DROP CONSTRAINT job_card_next_job_card_id_fkey;
  ALTER TABLE job_card
      ADD CONSTRAINT job_card_next_job_card_id_fkey
      FOREIGN KEY (next_job_card_id) REFERENCES job_card(job_card_id) ON DELETE SET NULL;
  -- same for prev_job_card_id
  -- partial_dispatch: preserve audit trail, block deletion
  -- (current NO ACTION is actually correct here; just document it)
  ```

### ME-03 — `record_output_v2` updates `qc_inspection` by matching `result='pending'` but does not scope by inspection_type or timestamp

- **File:** `app/modules/production/services/job_card_engine.py:540-557`
- **Category:** bug
- **Description:** The UPDATE is
  ```sql
  UPDATE qc_inspection SET … WHERE job_card_id = $1 AND result = 'pending'
  ```
  If a job card has multiple pending inspections (pre_production + in_process +
  post_production), this updates all of them with the same result, inspector, and
  timestamp. That is almost certainly not intended.
- **Fix:** Either require `qc.inspection_id` from the caller, or narrow the WHERE to
  a specific `checkpoint_type` matching the stage (`jc['stage']` → map to checkpoint).
  If the intent is to mark a single output-time QC, restrict with
  `AND checkpoint_type = 'post_production'` and `ORDER BY created_at DESC LIMIT 1`
  pattern (note: asyncpg UPDATE doesn't support ORDER BY + LIMIT directly, use CTE).

### ME-04 — `dispatch_partial_to_next_stage` does not mirror `fg_sku_name` between stages; floor movement logs potentially wrong SKU

- **File:** `app/modules/production/services/job_card_engine.py:1053-1062`
- **Category:** bug / audit
- **Description:** `INSERT INTO floor_movement (…, sku_name, …) VALUES (…, jc['fg_sku_name'], …)`
  uses the **FG SKU** of the source JC. For intermediate WIP (sorted-but-not-roasted,
  say), the material in motion is not the FG — it's a work-in-progress variant. Floor
  inventory reconciliation against this table will look incorrect because all stages
  tag their movements with the FG name.
- **Fix:** Either introduce a `wip_sku_name` convention (e.g. `"WIP-{fg_sku}-{stage}"`)
  or accept the FG-name convention and document it. At minimum add `reason =
  'stage_handoff:' || from_stage || '→' || to_stage` so the audit trail distinguishes
  WIP from FG dispatches.

### ME-05 — `create_job_cards` production-order numbering uses `COUNT(*)+1`; same race as HI-04

- **File:** `app/modules/production/services/job_card_engine.py:42-44`
- **Category:** concurrency / bug (pre-existing, not chain-specific, but blast radius
  grew because chain features sit on top of these IDs)
- **Description:** `prod_order_number = f"PRD-{year}-{seq:04d}"` where `seq =
  SELECT COUNT(*)+1 FROM production_order`. Two parallel plan executions collide. Because
  `prod_order_number` is `UNIQUE`, the loser raises.
- **Fix:** `CREATE SEQUENCE production_order_yearly_seq;` and use `nextval`.

---

## Low (4)

### LO-01 — `chain` endpoint returns null for `batch_size_kg` on falsy (=0) values

- **File:** `app/modules/production/router.py:814`
- **Category:** bug / correctness
- **Description:** `float(r['batch_size_kg']) if r['batch_size_kg'] else None` — the
  `else None` branch fires for `batch_size_kg = 0`, which is unlikely but possible in
  test/seed data; response is misleading.
- **Fix:** Use explicit NULL test: `... if r['batch_size_kg'] is not None else None`.

### LO-02 — Error messages leak quantities in logs/responses

- **File:** `app/modules/production/services/job_card_engine.py:1030`
- **Category:** info-disclosure (design awareness)
- **Description:** `"qty_kg 400.0 invalid. Remaining to dispatch: 150.0 kg"` leaks
  server-side quantity state back to the caller. Per the prompt, probably acceptable,
  but worth noting: if the caller is a customer-facing UI, the remaining-kg number
  reveals production batch size. Keep it, but flag in API docs as "authenticated-only
  endpoint returns operational details in error strings."
- **Fix:** No code change; document in API spec.

### LO-03 — Dead code: `record_output` delegates to `record_output_v2` with no migration plan

- **File:** `app/modules/production/services/job_card_engine.py:440-442`
- **Category:** code quality
- **Description:** `record_output` is a one-line passthrough to v2. Either remove it and
  update call sites (`mcp_server.py:1065-1068` still constructs a v1-shaped dict with
  dropped fields) or keep it and document the v1-compat contract.
- **Fix:** Delete v1 wrapper, update callers. `mcp_server.py:1068`'s `data` dict still
  contains `material_return_kg`, `rejection_kg`, `rejection_reason`, `process_loss_pct`,
  `offgrade_kg`, `offgrade_category` — none of these are read by `record_output_v2`, so
  they are silently dropped (data-loss adjacent, but MCP is internal).

### LO-04 — `job_card_partial_dispatch.dispatched_by` is `TEXT` (nullable), giving fully anonymous audit rows

- **File:** `app/db/001_job_card_chain.sql:34`
- **Category:** audit / design
- **Description:** The migration declares `dispatched_by TEXT` (no NOT NULL). Combined
  with CR-03 (caller-provided, unauthenticated), the audit log is unreliable.
- **Fix:** After auth is in place, `ALTER TABLE job_card_partial_dispatch ALTER COLUMN
  dispatched_by SET NOT NULL;` and populate from principal.

---

## Design Questions for the Author

Because this feature shipped without a design doc, the following decisions are implicit
in the code and need to be made explicit **before production**:

1. **Cycle detection policy.** Is the chain always a simple linear sequence built once
   by `create_job_cards`, or can it be re-wired later (e.g., after a stage is skipped
   or a JC is cancelled and replaced)? If re-wireable, where is the single writer, and
   what prevents cycles? (Today: nothing. See CR-04.)

2. **Concurrency model for dispatch.** What guarantees does `dispatch_partial_to_next_stage`
   make about simultaneous calls? Is the intended semantic "first caller wins, second
   sees updated remaining" (requires row lock) or "both fail with conflict" (requires
   optimistic `WHERE dispatched_to_next_kg = $expected`)? Pick one and encode it. (Today:
   neither — both succeed and the invariant breaks. See CR-02.)

3. **Column-drop migration path.** The migrate file drops 7 columns and comments "data
   migration was run manually on 2026-04-10." Where is that script, what did it do, and
   what happens when this repo is deployed to a fresh prod DB where the "manual" step
   was never run? Recommend: commit the data-migration SQL with idempotence guards, and
   document a dry-run procedure. (See CR-01 and migrate.sql:479.)

4. **Permission model.** Who can read `/orders/{id}/job-card-chain`? Who can POST to
   `/job-cards/{id}/dispatch-to-next`? Is `dispatched_by` (a) self-declared by the
   client, (b) derived from a JWT principal, (c) scanned from a team-leader QR badge?
   The current code says (a), which is unauthenticated and forgeable. (See CR-03, LO-04.)

5. **`fg_sku_name` vs WIP naming in floor_movement.** Between stages, what's actually
   moving isn't the FG — it's WIP. Should `floor_movement.sku_name` reflect that?
   (See ME-04.) This touches inventory reconciliation (`day_end` balance scan).

6. **Dispatch-vs-output ordering rule.** Should an operator be allowed to dispatch WIP
   to next stage *before* recording `fg_actual_kg` on the current stage? Today: yes,
   because `record_output_v2` is not a prerequisite. Likely wrong for audit cleanliness.
   (See HI-02.)

7. **Terminal-state handling on next JC.** What happens if next JC was force-cancelled
   by supervisor while current JC is still in progress? Should current JC's dispatch
   fail, or should dispatch re-route to the next-next stage, or should current JC be
   auto-cancelled? Today: dispatch silently succeeds into a dead JC. (See HI-01.)

8. **Indent lifecycle on floor shortfall.** `check_and_raise_floor_indents` writes
   `status='draft'`. Is the intent for a human to review each draft before it goes to
   purchase, or should it auto-promote to `'raised'`? Today the draft never escapes
   draft state. (See HI-03.)

Documenting these choices in a short spec (`docs/superpowers/specs/2026-04-NN-job-card-chain-design.md`)
and running a test pass that exercises at least: (a) concurrent dispatch, (b) dispatch
to cancelled next JC, (c) cross-entity chain attempt, (d) column-drop on a prod-shaped
DB — would de-risk this feature meaningfully.

---

_Reviewed: 2026-04-18_
_Reviewer: Claude (gsd-code-reviewer, Opus 4.7)_
_Depth: standard_
