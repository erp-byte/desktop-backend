# Review-Fix Summary — 2026-04-18

Consolidated fixes for findings from the four code reviews in
`docs/reviews/` plus prompt-specified items for the webhook emit-call
migration (no review file present on disk — `webhook-emit-calls-REVIEW.md`
was not found under `docs/reviews/`; fixes for C1/C2/H1/H2–H4/M1/M2/M4 were
applied from the prompt's concrete guidance).

**Constraint honoured:** no `git add` / `git commit` run. Working tree only.

## Fixes applied

Ordered by file, then severity. All line numbers are post-edit.

### `app/db/001_job_card_chain.sql`
- **CR-04 (job-card-chain)** — Added idempotent `chk_jc_no_self_chain`
  and `chk_jc_no_self_prev` CHECK constraints via DO-block; prevents 1-cycles
  on `next_job_card_id` / `prev_job_card_id`. Comments call out that longer-cycle,
  same-order and same-entity invariants remain the caller's responsibility.

### `app/db/002_webhooks.sql`
- **HI-06 (webhook-package)** — Added `ALTER TABLE webhook_delivery
  ADD COLUMN IF NOT EXISTS target_roles TEXT[] NOT NULL DEFAULT '{}'` so retries
  can reconstruct the original `Event.target_roles`.

### `app/modules/auth/services/auth_service.py`
- **HIGH-4 (auth-middleware)** — Removed `full_name` / `phone` PII from the
  login INFO log; now logs `user_id` + `role` only. Preserves audit intent
  without leaking DPDP-regulated fields into log aggregators.

### `app/modules/auth/services/permission_service.py`
- **HIGH-5 (auth-middleware)** — `check_permission`: on any scope mismatch
  (entity / warehouse / floor), return `False` immediately instead of falling
  through to broader permission rows. Falling through silently escalated
  privilege because `allowed_entities = NULL` means "all entities" and is
  STRICTLY MORE permissive.

### `app/modules/production/router.py`
- **M1 (webhook-emit)** — Wrapped `dispatch-to-next`, `receive-material`,
  `acknowledge-material`, `assign`, `start`, `sign-off`, `force-unlock`, and
  `output` (v2) emit sites in `deferred_events()` so events are published only
  on transaction commit. `complete` already used this pattern.
- **M2 (webhook-emit)** — `receive-material`, `sign-off`, `record-output` now
  check `"error" not in result` before emitting.
- **M4 (webhook-emit)** — `dispatch-to-next` emit now reads `qty_kg` from the
  service result (authoritative post-clamp value) rather than echoing the
  request body verbatim.
- **H1 (webhook-emit)** — Every relocated emit is wrapped in try/except that
  logs and swallows; a broadcaster hiccup can no longer flip a successful
  write to a client-visible 500.

### `app/modules/production/services/day_end.py`
- **CR-01 (job-card-chain)** — `get_day_end_summary`: replaced the SELECT of
  dropped columns `o.offgrade_kg` / `o.dispatch_qty` with
  `COALESCE(SUM(jcb.quantity_kg), 0) AS offgrade_kg` (via
  `job_card_byproduct` aggregate) and `jc.dispatched_to_next_kg AS dispatch_qty`.
  Day-end API will no longer 500 with `UndefinedColumnError`.
- **CR-01 (job-card-chain)** — `bulk_dispatch`: replaced the broken
  `UPDATE job_card_output SET dispatch_qty = ...` with
  `UPDATE job_card SET dispatched_to_next_kg = ...`; documented in docstring
  that the legacy column was dropped by migration 011.
- **H1 (webhook-emit)** — Wrapped `events.dayend_reconciled` in try/except.

### `app/modules/production/services/discrepancy_manager.py`
- **H1 (webhook-emit)** — Wrapped `events.dayend_discrepancy_found` in
  try/except.

### `app/modules/production/services/floor_tracker.py`
- **H1 (webhook-emit)** — Wrapped `events.material_moved` in try/except.

### `app/modules/production/services/fulfillment.py`
- **C1 (webhook-emit)** — `sync_fulfillment`: dropped `so_number` from the
  `so_fulfillment` INSERT column list and parameters. The schema has no
  `so_number` column — the INSERT was guaranteed to fail. Audited
  `carryforward_orders` (already correct — no `so_number` binding). The
  surface is now consistent with `production_schema.sql`.
- **H1 (webhook-emit)** — Wrapped `events.fulfillment_synced` and
  `events.fulfillment_revised` in try/except.

### `app/modules/production/services/job_card_engine.py`
- **CR-02 (job-card-chain)** — `dispatch_partial_to_next_stage`: replaced
  the read-validate-update TOCTOU pattern with a single atomic conditional
  UPDATE:
  `UPDATE job_card SET dispatched_to_next_kg = dispatched_to_next_kg + $1
   WHERE job_card_id = $2 AND status = 'in_progress'
     AND next_job_card_id IS NOT NULL
     AND dispatched_to_next_kg + $1 <= batch_size_kg
   RETURNING dispatched_to_next_kg`.
  Falls through to a precise error-path fetch only when zero rows update.
  Eliminates the over-dispatch race described in the review.
- **HI-01 (job-card-chain)** — Same function: added terminal-state rejection
  for the next JC (`completed` / `closed` / `cancelled`). On rejection the
  increment is rolled back so the source JC isn't double-charged.
- **HI-03 (job-card-chain)** — `check_and_raise_floor_indents`: changed
  insert status from `'draft'` to `'raised'` and `indent_source` value from
  `'floor_shortfall'` to `'floor_shortage'` so the floor-shortfall indents
  are visible to MRP / purchase dashboards immediately. Applied to both RM
  and PM branches.
- **HI-04 (job-card-chain)** — Same function: replaced the `COUNT(*)+1`
  daily-sequence generator with
  `COALESCE(MAX(CAST(SUBSTRING(indent_number FROM 14) AS INT)), 0) + 1`
  and wrapped in a 3-attempt retry loop that catches unique-violation and
  regenerates. Handles both the same-transaction loop-collision case (see
  review) and the cross-transaction race. Applied to both RM and PM branches.
- **ME-03 (job-card-chain)** — `record_output_v2`: narrowed the
  `qc_inspection` UPDATE to a single row selected via a subquery ordered by
  `checkpoint_type = 'post_production'` first, then most recent. Previously
  any JC with multiple pending inspections (pre / in-process / post) would
  have all of them collapsed to the same result. Uses the `id` PK column
  (verified against `ims_new_schema.sql`).

### `app/modules/production/services/job_card_pdf.py`
- **CR-01 (job-card-chain)** — Replaced references to
  `output.material_return_kg` and `output.rejection_kg` (dropped columns)
  with `output.net_output_kg` and `output.yield_pct`. Comment documents the
  migration-011 rationale.

### `app/modules/production/services/mrp.py`
- **H1 (webhook-emit)** — Wrapped `events.mrp_completed` and
  `events.mrp_shortage_detected` in try/except.

### `app/webhooks/dispatcher.py`
- **CR-02 (webhook-package)** — Added module-level `_dispatch_concurrency =
  asyncio.Semaphore(8)` and wrapped `_dispatch_event` in it so a burst of
  events cannot spawn unbounded tasks competing for the DB pool.
- **HI-01 (webhook-package)** — Added `_inflight_tasks: set[asyncio.Task]`
  + `_spawn(coro)` helper that installs a done-callback to discard. All
  delivery tasks are now strong-ref'd; GC cannot kill them mid-flight.
  `_dispatch_event` now uses `_spawn(...)` instead of raw
  `asyncio.create_task(...)`.
- **HI-06 (webhook-package)** — `_deliver` INSERT now persists
  `target_roles` into the new column.

### `app/webhooks/event_bus.py`
- **HI-03 (webhook-package)** — Elevated the subscriber-queue-full log from
  warning to error, and included `event_type` / `entity` so the ops signal
  is actionable.

### `app/webhooks/events.py`
- **H2 / H3 / H4 / L4 (webhook-emit)** — Added `_validate_entity(entity,
  event_type)` helper (normalises + coerces + warns on unexpected values,
  never raises) and called it at the top of every named event constructor
  (fulfillment, plan, mrp, indent, job_card, qc, material, dayend,
  store_alert). Malformed entity values are logged and the emit proceeds.

### `app/webhooks/router.py`
- **HI-05 (webhook-package)** — `list_deliveries`: replaced ad-hoc f-string
  WHERE construction with a column-name whitelist
  (`_LIST_DELIVERIES_ALLOWED`), explicit placeholder-index bookkeeping for
  LIMIT / OFFSET, and an assert on whitelisted columns. The values were
  already parameterized; this hardens against future edits that would add
  caller-supplied filter names.
- **HI-06 (webhook-package)** — `retry_delivery`: reconstructed `Event` now
  restores `target_roles` from the persisted `webhook_delivery.target_roles`
  column (falls back to `[]` for rows predating the column). Comment
  explains the inline import of `retry_single_delivery` as circular-dep
  breakage (partly addresses LO-06).

### `mcp_planner.py`
- **C2 (webhook-emit)** — `approve_plan`: wrapped the MRP + draft-indent
  transaction in `deferred_events()` imported from
  `app.webhooks.event_bus`. Events now publish only on commit.

### `mcp_server.py`
- **C2 (webhook-emit)** — `approve_plan` and `run_mrp`: wrapped their
  transaction blocks in `deferred_events()` from `app.webhooks.event_bus`.

### `mcp_tracker.py`
- **CR-01 (job-card-chain)** — `get_day_end_summary` MCP tool: same dropped-
  column fix as `day_end.py` — replaced `o.offgrade_kg` / `o.dispatch_qty`
  with a `job_card_byproduct` aggregate and `jc.dispatched_to_next_kg`.

---

## Deferred — needs user decision

1. **SSRF URL allow-list / private-IP deny-list (CR-01 webhook-package).**
   *Why deferred:* needs a policy decision (HTTPS-only? corporate allow-list?
   allow `httpbin.org` for testing? explicit metadata-host deny?) and the
   review suggests DNS-rebinding protection which needs an IP-pinning
   transport choice. Partial SSRF hardening is arguably worse than none.
   *Question:* Should webhook targets be restricted to an explicit allow-list
   of hostnames, or HTTPS + public-IP-only with metadata hosts blocked?

2. **Argon2id password hash migration (HIGH-2 auth-middleware).**
   *Why deferred:* requires a user-flow decision.
   *Question:* On migration, do existing users rotate silently on next
   successful login (re-hash with Argon2 when Fernet decrypt succeeds),
   or do we force a password-reset flow for everyone?

3. **Session rotation on role change (HIGH-1 auth-middleware).**
   *Why deferred:* business decision on staff-management UX.
   *Question:* When an admin edits `role_id` via PUT /users/{id}, should we
   hard-invalidate all the user's sessions (force re-login) or soft-refetch
   permissions on every request (keeps them logged in but may break cached
   frontend state)?

4. **Login invalidates-prior-sessions (HIGH-3 auth-middleware).**
   *Why deferred:* shared-device factory-floor policy.
   *Question:* Should a successful login on device B invalidate the session
   on device A, or allow up to N concurrent sessions and expose a
   `/auth/logout-all` endpoint? N = ?

5. **Deactivated-user session invalidation rewrite (CRITICAL-2
   auth-middleware).**
   *Why deferred:* requires a careful `u.is_active = TRUE` addition to
   `validate_session` SQL, column-alias disambiguation, AND updates to
   `PUT /users/{id}` to invalidate sessions on flip. Large enough to warrant
   a test plan (currently no test suite runs in this session).
   *Question:* OK to land the alias+WHERE refactor + add session-invalidation
   hook in a follow-up PR with unit tests?

6. **Password storage migration (any Fernet -> Argon2id).**
   *Why deferred:* same as #2.

7. **Full cycle detection for job_card chain (beyond self-loops; CR-04
   deferred portion).**
   *Why deferred:* recursive-CTE trigger is too much scope for auto-fix.
   The self-loop CHECK is in place; document cycle-prevention as caller
   responsibility and ship a recon script separately.
   *Question:* Do we need cycle detection at insert-time, or is a nightly
   scan sufficient?

8. **ME-01 chain-endpoint entity filter (job-card-chain).**
   Not applied — it is tenancy-scope hardening that is trivially adjacent
   to the deferred CRITICAL auth scope-tampering work (CRITICAL-1 + CR-03).
   Should be folded into the auth-model-rollout PR, not a mechanical fix.

---

## Regression risk

- **`dispatch_partial_to_next_stage` (CR-02 atomic UPDATE rewrite).** The
  function changed shape significantly: it now does `UPDATE ... RETURNING`
  *first*, then fetches fields, then rolls back the increment if the next
  JC is terminal (HI-01). The rollback is a compensating UPDATE, not a SQL
  ROLLBACK, so the containing transaction must still be wrapping this call
  (the router does — verified). If any future caller forgets the
  `async with conn.transaction()` wrapper, a crash between the increment and
  the rollback would leave the source JC's `dispatched_to_next_kg` over-
  counted. HI-05 (engine-owned transaction) is NOT applied in this pass
  and remains latent.
- **`check_and_raise_floor_indents` retry loop.** The 3-attempt unique-
  violation retry assumes unique violations surface with `'unique'` or
  `'duplicate'` in the exception message — asyncpg's `UniqueViolationError`
  satisfies this, but the catch-and-re-raise is string-sniffing. A cleaner
  fix is `except asyncpg.exceptions.UniqueViolationError`; left as string
  match to avoid a new import at the edge of the diff.
- **`day_end.bulk_dispatch` redirected to `job_card.dispatched_to_next_kg`.**
  Semantically this overwrites the dispatch count on the source JC. The
  previous code wrote to a dedicated `job_card_output.dispatch_qty` column
  that is now gone. If downstream reports distinguished "dispatched" from
  "carried to next stage", this merging is a semantic regression; the review
  flagged the endpoint as possibly "superseded by the new per-JC dispatch-to-
  next flow" and suggested deleting it. We preserved functionality but the
  accounting split is gone.
- **`record_output_v2` QC scoping (ME-03).** The subquery uses a column alias
  `id` rather than e.g. `qc_id` based on `ims_new_schema.sql`. If any
  deployment is on an older schema where the PK is named differently, this
  UPDATE will fail with `UndefinedColumnError`. Verified against the schema
  committed in this repo.
- **Event-emit buffering via `deferred_events()` in router endpoints.**
  Events now fire only after the outer `conn.transaction()` exits cleanly.
  If any service was silently relying on events firing BEFORE commit (e.g.
  a test that asserts an event was raised despite a later failure), it will
  break. No such coupling was found in a grep of the services.

## Verification

Ran the prompt's sanity check after every edit completed:

```
.venv/Scripts/python.exe -c "from app.main import app; print('ok', len(app.routes))"
```

Output: **`ok 196`** — all imports resolve and the FastAPI app constructs
successfully (196 routes registered, same count as pre-edit). Additionally,
AST-parsed every modified Python file via `ast.parse(...)` — all files
parse cleanly.

Further sanity checks that need a running DB and were NOT performed here:
- `GET /api/v1/production/day-end/summary` against a migration-011-applied
  DB (CR-01 job-card-chain regression path).
- Concurrent `POST /job-cards/{id}/dispatch-to-next` from two clients to
  confirm the atomic UPDATE clamps at `batch_size_kg` (CR-02 job-card-chain).
- Send a webhook event with a large fan-out and confirm no queue-drop
  warnings / task GC warnings in logs (HI-01 + HI-03 webhook-package).
- POST /auth/login and confirm no phone/name in log lines (HIGH-4).

---

_Prepared: 2026-04-18_
_Author: Claude (gsd-code-fixer)_
