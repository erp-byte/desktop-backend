# CS02 BOM Change Control: FRD + LLD Review

**Date:** 2026-10-30
**Reviewed:** "BOM Change Control FRD (CS02)" v0.1 and "BOM Change Control LLD (CS02)" v0.1
**Checked against:**
- SAP CS02 spec (`CS02_Change_Material_BOM.md`).
- The fit-gap [2026-10-30-cs02-bom-change-fit-gap.md](2026-10-30-cs02-bom-change-fit-gap.md).
- Code on the commit the LLD names: `wt-jc-be` @ `6c91f04`, which contains `bec27e5` and is 15 commits ahead of it, plus `wt-jc-fe` @ `99f67ed`.

**Not checked:**
- The BOM Module FRD/LLD. They were not provided, and this LLD relies on them for `bom_audit_log`, `status`, `created_by`, `source`, `change_request_id` and item nodes.
- RDS.

## 1. Verdict

**The FRD is sound.**
- Every critical fit-gap item has a requirement.
- The scope cuts are right for Candor: ECR/ECO, edit locks, lot sizes and low-level codes.
- The apply design fixes the actual defects: full-column copy, base check, exactly one row changed per operation, one transaction.

**The LLD cannot be built as written for Phase 0.**
- It names functions, routes, columns and statuses that don't exist.
- It assumes a permission check that can ignore admin, which the current auth code cannot do.
- It needs columns that only the unseen BOM Module LLD creates.
- It has four logic bugs. A NULL reason passes the CHECK. Line numbers can be reused. `version + 1` can collide. Repointing can silently move old job cards.
- The two documents disagree on what is in Phase 0 (§4).

Fix B1–B10, resolve §4 into one Phase 0 list, and issue LLD v0.2 before building.

## 2. Fit-gap facts re-verified on main

The FRD treats findings from the old checkout "as true until re-verified on main". They hold:

| Fit-gap finding | On `6c91f04` / `99f67ed` |
|---|---|
| `_apply_permanent_bom` copies 10 header and 10 line columns, no route, no line-ownership check | Unchanged (`amendments_v2.py:710-861`). The file's only changes since `bec27e5` are in plan delete and plan field change. |
| W2 deactivates and versions with `ILIKE` | Still there (`plan_v2.py:319`, `:324`). The route is gated on `production.plans.create` (`router.py:5790-5794`). |
| UI keys mismatch; `bom_id` never sent | Unchanged. The UI requires `material_code/qty` (`_AmendmentsTab.tsx:637-639`). The POST body has no `bom_id` (`:660-665`). |
| Only the amendment apply takes the per-FG lock | True. `pg_advisory_xact_lock(hashtext(fg))` exists only at `amendments_v2.py:762`. |

**LLD "confirm" markers this answers:**

| LLD marker | Answer |
|---|---|
| Constraint name `bom_amendment_request_v2_request_type_check` | Correct (`production_jobcard_slice.sql:206`) |
| `request_id` type | `BIGINT` (`028:81`). An FK from `bom_header.change_request_id` must also be `BIGINT`. |
| `req.maker_id`, `req.checker1_id` | The real names are `maker_user_id`, `checker1_user_id`, `checker2_user_id` (`028:99-101`) |
| Plan-line table | `production_plan_line_v2`, `plan_line_id BIGSERIAL` (`009:151`) |
| Plan-line status values | `planned`, `in_progress`, `completed`, `cancelled` (`009:168-170`). There is no `approved`. |
| `auth_permission` shape | `(module, sub_module, sub_sub_module, action)`, UNIQUE, with no CHECK on `action` (`auth_schema.sql:50-58`). The `make/check1/check2` rows insert as written. |
| Q-12: can a user hold several roles? | Yes. `AuthUser.role_ids` exists, and `check_permission` grants if any role allows (`middleware.py:65-70`, `permission_service.py:15-18`). `router_amendments._has_role` still checks `role_name` only, so it is already wrong for multi-role users. |
| Roles | These exist: `npd_team` (granted in `075`), `qc_manager` (`auth_schema.sql:104`), `production_manager` (`028:216`). `bom_owner` and `auditor` do not. |

**New defect.** The DB constraint lists 12 request types and leaves out `plan_field_change` (`028:82-95`; dump `:206`), but the Python `REQUEST_TYPES` accepts it. On any database built from the migrations, creating a `plan_field_change` request fails the CHECK. The LLD's constraint rewrite (C0.4) must list all 13 current types plus the new one. Read the RDS constraint first, because RDS may differ.

## 3. Blocking issues

| # | Where | Problem | Fix |
|---|---|---|---|
| B1 | LLD §2, §5, §8, C0.3 | Phase 0 depends on objects only the BOM Module LLD defines: the `bom_audit_log` columns and `bom_header.created_by / source / change_request_id`. LLD §2 adds only `superseded_at`, yet FR-B-08 (author and source on every version) is Phase 0. | Put the Phase 0 DDL in this LLD: `bom_audit_log`, `created_by INT`, `source TEXT`, `change_request_id BIGINT`. Otherwise name the BOM Module LLD step and its order relative to C0.3. |
| B2 | LLD §2 `ck_amend_bom_change_fields` | `length(btrim(reason)) >= 20` is NULL when `reason` is NULL, and a CHECK that evaluates to NULL passes. A BOM change with no reason is accepted. | `reason IS NOT NULL AND length(btrim(reason)) >= 20` |
| B3 | LLD §5 `add` | The next `line_number` is computed on the copy after earlier `remove` operations have run. Removing the last line and adding one in the same request gives the new component the removed line's number. Before item nodes exist, `line_number` is the node (FR-A-05), so the "same item" across versions becomes a different material. | Take the next number from `MAX(line_number)` of the base version (or the whole lineage), computed once before any operation runs. |
| B4 | LLD §5 `version = base.version + 1` | `uq_bom_header_fg_version` is unique on (FG, version). If any row for the FG has a higher version than the current one (W2's ILIKE lineage, NPD supersede, a manual reactivation), the insert collides. Today's code uses `MAX(version) + 1` under the lock for this reason (`amendments_v2.py:770-774`). | Keep `MAX(version) + 1` under the lock |
| B5 | LLD §7, "Use v(n+1)" switch | `backfill_indents_for_jc` (`job_card_v2.py:656`) uses the card's `bom_id`, falls back to the plan line's when it is NULL, and writes that value onto the card (`:705-712`). Switching a partly carded line whose older cards have NULL `bom_id` moves those cards to the new recipe the next time indents are backfilled, with no audit. | Before any repoint, copy the line's current `bom_id` onto its cards that have NULL. Repoint only after that. Add a test. |
| B6 | LLD §7, §10 | `job_card_v2.create_from_plan_line` and `POST /production/job-cards` don't exist. Job cards are created by `create_job_cards_for_line` (`job_card_v2.py:1152`; `POST /plans-v2/lines/{plan_line_id}/job-cards`, `router.py:5198`) and by `create_job_cards_from_plan` (`job_card_v2.py:882`). Both read the line's `bom_id` (`:904`, `:1286`). The repoint SQL filters on `status IN ('planned','approved')`, but `approved` is not a line status, and its `NOT EXISTS` ignores `deleted_at`. | Put the `bom_superseded` check in both creation functions. Use `status IN ('planned','in_progress')` and `jc.deleted_at IS NULL`. |
| B7 | LLD §10 `can_act`, FR-R-13 | "No admin shortcut" cannot be met with today's auth code. `require_permission` returns immediately for admins (`middleware.py:160`), and `check_permission` returns True for admins (`permission_service.py:29-30`). `user.has_permission()` does not exist. `check_permission` also falls back from `(production, amendments, permanent_bom_change, check1)` to `(production, amendments, NULL, check1)` and then to `(production, NULL, NULL, check1)`. | Write `can_act` as its own check. It calls `check_permission(conn, user.role_ids, is_admin=False, …)` and does not let admin pass. Seed no broader `check1/check2` rows. |
| B8 | LLD §9 `bom_txn` | The helper is written for psycopg: sync `@contextmanager`, `with conn.transaction()`, `%s` placeholders. The stack is asyncpg, which needs `@asynccontextmanager`, `async with`, and `$1`. The helper is called `set_actor()` in §5 and §8 and `bom_txn()` in §9. | Use one async helper with one name. Inside the approval transaction it opens a savepoint, and `set_config(…, true)` lasts until the outer transaction ends, which is what the trigger needs. |
| B9 | LLD §6 schema vs today's columns | `AddLine.material_sku_id` cannot be stored: `bom_line` has `material_sku_name` and no `sku_id` until P2-2. `route_step_no`, `fixed_qty` and `provision` have no column, and `LINE_FIELDS` includes `route_step_no`. `RouteChange` is never defined. `quantity_per_unit` allows 6 decimal places while the column is `NUMERIC(15,3)`. With `extra='forbid'`, Phase 0 clients and this schema can't agree. | Publish a Phase 0 schema. At create, resolve `material_sku_id` to `all_sku.particulars` and check its type. Drop fields that have no column. Cap at 3 decimal places (the precision guard). Define `RouteChange` or defer it (B10). |
| B10 | FRD §6 `route` operation, §11 `step_in_use` | "Steps referenced by lines cannot be removed" has nothing to check against. `bom_line` and `bom_process_route` share no key (`bom_aggregate_service.py:11-25`); `route_step_no` is a BOM Module LLD column. | Defer `route` operations until `route_step_no` exists. Alternatively, define "referenced" as `consumed_at_stage = practical_operation` and accept that it is fuzzy. |

## 4. FRD ↔ LLD disagreements

| Topic | FRD | LLD | Resolve to |
|---|---|---|---|
| Multi-change, one version | FR-A-04, Phase 1 | The list-based `permanent_bom_change` is the only new type, in Phase 0 (D3, §9 W3) | Phase 0 |
| Mandatory reason | FR-R-04, Phase 1 | DB CHECK in C0.4 (Phase 0) | Phase 0 |
| `new_bom_id` on the request | FR-A-07, Phase 1 | Added in C0.4 and set by `mark_applied` (Phase 0) | Phase 0 |
| No-op refused | FR-R-14, Phase 2 | Create-stage check (Phase 0) | Phase 0. It is a comparison against a base that is already loaded. |
| Delimit and `superseded_at` | FR-A-08, Phase 1 | `delimit()` runs in every apply, but the column arrives in C1.1 | Either the Phase 0 apply skips `superseded_at`, or C1.1 moves to Phase 0 |
| Supersede link | `superseded_by_bom_id` on the base | `supersedes_bom_id` on the new version (EXCLUDE list), and absent from the §2 DDL | Pick one. `supersedes_bom_id` plus `change_request_id` on the new row is enough. |
| Before/after diff | FR-R-08 in Phase 1, but P2-4 (diff view) in Phase 2 | `bom_diff.compare` runs when a checker opens the request | Phase 1 |
| Who sets `base_bom_id` | FR-R-02 and the payload example: the client sends it | §10: the server sets `base_bom_id = bom_id` | The client sends the `bom_id` it displayed. The server refuses `bom_not_current` if that is not current, then sets base = `bom_id`. Remove `base_bom_id` from the client payload. |
| Line key in the payload | The example uses `"item_node_id"` | The schema uses `node` with `extra='forbid'` | As written, the LLD schema rejects the FRD example. Use `node` (Phase 0 = `line_number`). |
| Reject | FR-R-09: the request returns to the maker as a draft | Not designed. Today `rejected` is terminal (`amendments_v2.py:80`). | Keep reject terminal (the maker re-raises, linked to the rejected request), or design the transition |
| Withdraw | FR-R-10: any time before final approval | Not designed. Today it is allowed only in `draft` or `pending_review` (`amendments_v2.py:587`). | Decide which and state it |
| Serialising writers | FR-A-10 says "kept from today" for every writer | Only the apply takes `lock_key` | Today only the amendment apply locks. Add the same key lock to W1, W2 and W4, or reword FR-A-10. |
| Roles | §4 says it uses "roles that already exist" | — | `bom_owner` and `auditor` are new. Seed them in C0.4. |
| Tolerance range | Header op allows 0 < x < 1 | `HEADER_FIELDS` includes the tolerance | Reuse W1's `0 ≤ x ≤ 1.0` (`bom_write_service.py:193-210`) so there is one rule |
| Tests before Phase 0 | Exit gate is T1–T7 and T9–T11 | §12 says "the 15 fit-gap tests are automated before Phase 0 ships" | T12–T15 belong to Phase 1 |

## 5. Not covered: SAP spec and fit-gap

| Source | Gap |
|---|---|
| SAP §5.1 `C_STUE_NOH`; FRD §10 last line | The firefighter permission `bom.change.emergency` has no endpoint, time-box or next-day review report in the LLD |
| SAP §5.3 SoD-4; fit-gap #10 | The FRD's weekly exception report (a maker who closed a job card on the same FG within 7 days) has no design |
| Fit-gap #44; FRD Q-5 default | The FRD says the migration creates the unique indexes after a duplicate check, but no C-step does that. Add one after Q-5/Q-15 and before C0.4. |
| SAP §11 C-19 | No warning when an SFG is used elsewhere |
| SAP §11 C-20 | `recursive_bom` is listed, but SFG lines name SFG codes made inside the same BOM's route. There is no cross-BOM SFG graph to check. Define what a cycle is, or drop the message. |
| Fit-gap Q-3 | Who holds `production.plans.create` on RDS. It is missing from FRD §13, and it sizes the W2 exposure until C0.5 ships. |
| None (new) | Nothing checks that each entity has at least one active `qc_manager` and one `production_manager`. Without them, the Phase 0 chain blocks every BOM change. Add this check to C0.1. |
| LLD §6 precision guard | "Added to ingest": `master_ingest` runs at server startup. At ingest, a quantity with 4 decimal places must be reported and skipped, not raised, or startup ingest breaks. |

## 6. Smaller issues

- **§5 copy.** `EXCLUDE` for `bom_line` and `bom_process_route` keeps `created_at`, so new rows carry the base version's timestamps. Exclude `created_at`.
- **§6 `field` / `header` operations.** `value: Any` needs a typed validator per allowed field: `floors` is `TEXT[]`; `loss_pct` and `offgrade_max_pct` are `NUMERIC(5,3)`.
- **§6 duplicate operations.** Two `qty` operations on one node, or `remove` then `qty`, should be refused at create, not left to fail at apply.
- **§8 trigger.**
  - `REVOKE UPDATE, DELETE` does nothing if the app connects as the table owner; the append-only trigger is the real control.
  - `bom_audit_log.bom_id` must not have an FK to `bom_header`, or auditing a DELETE fails.
  - The audit column `request_id` (the HTTP request id) has the same name as the ledger's `request_id` (the amendment id). Rename it `http_request_id`.
- **§9 lint rule.**
  - `app/bom/` doesn't exist; the module is `app/modules/bom/`.
  - The pattern `INSERT INTO bom_` also matches `bom_amendment_request_v2`, `bom_audit_log` and `bom_plan_repoint_log`. 12 Python files match today, including the ledger's own.
  - The repo has no CI (no `.github`). Write the rule as a pytest static check, like the existing migration tests.
- **C0.2 repair.** A later amendment copied from a damaged version inherits the damage. Repair version by version down each lineage, not only the versions created directly by an applied request.
- **C0.5 rollback.** "Flag off restores today's endpoints" brings back the apply that damages versions. Flag off should disable BOM amendments instead.
- **C0.6 conversion.** Old requests store `bom_line_id`. Converting one needs `bom_line_id → line_number` on its own `bom_id`, and must refuse when the line is not on it (the #39 defect).
- **§12 tests.**
  - T4 and T9 need a writable Postgres. Repo tests use fake connections or read-only live checks (`*_live_sql.py`), and nothing writes to RDS. Name the test database.
  - T10 assumes Playwright, but `wt-jc-fe/package.json` has none.
- **`BOM_CHANGE_V2` flag.** There is no feature-flag mechanism in `app/core`, so this is a new settings entry.
- **FRD §6 example.** `quantity_per_unit: 50` KGS is SAP's dose per 1,000 kg base. In our model the quantity is per FG unit, so 50 means 50 kg per unit. Use a realistic value such as 0.050.

## 7. Next step

Issue LLD v0.2 with B1–B10 fixed and §4 resolved into one Phase 0 list. That version can then be turned into a task-by-task implementation plan with tests per task.
