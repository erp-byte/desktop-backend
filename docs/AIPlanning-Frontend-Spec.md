# AI Production Planning — Frontend Integration Spec

> Companion to `docs/JobCard-Frontend-Spec.md`. Covers the Claude-driven plan generation and revision endpoints. Adds the new `floor_qty_units` field on `create-with-ai`, and the brand-new `revise-with-ai` endpoint introduced 2026-05-07.

**Base path:** `/api/v1/production`
**Auth:** every request must carry `Authorization: Bearer <jwt-access-token>`. On 401, refresh and retry.
**Permissions enforced:**
- `create-with-ai` → permission `production/plans/create` (currently unprotected on backend, see "Cross-cutting" below)
- `revise-with-ai` → permission `production/plans/revise/create` (FastAPI dependency `require_permission`; 403 if missing)
- All `GET /plans*` → permission `production/plans/view`

**Conventions:**
- `R` = required (must be present, non-null)
- `O-null` = optional, may be omitted OR explicitly `null` (only documented where the backend distinguishes them)
- `O` = optional, may be omitted (server uses default)
- Quantities are kg unless field name says `_units` (whole-number unit count)
- 4xx response body is always `{"detail": "<message>"}` from FastAPI
- All datetimes are ISO 8601 UTC; all dates are `YYYY-MM-DD`

---

## Section 1 — Plan Generation
*Activity:* `PlanCreationActivity` — pick fulfillment items + floor/machine constraints + send to AI

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/plans/create-with-ai` | Generate a new plan via Claude AI |

### `POST /plans/create-with-ai`

**Request body:**

| Field | Type | Required? | Notes |
|---|---|---|---|
| `entity` | string | **R** | `cfpl` or `cdpl` |
| `plan_type` | string | O | Default `"daily"`. Stored on the plan; **the prompt path is currently always `daily` regardless of value** |
| `plan_date` | string (YYYY-MM-DD) | O | Default = today (server clock) |
| `plan_name` | string | O | Display only. If empty, server returns `"Daily Plan — <date>"` |
| `created_by` | string | O | Currently accepted but **not persisted** |
| `selected_items` | `CreatePlanItem[]` | **R** | Empty list → `400 "No items selected"` |

`CreatePlanItem`:

| Field | Type | Required? | Notes |
|---|---|---|---|
| `fulfillment_id` | int | **R** | FK to `so_fulfillment` |
| `custom_qty_kg` | number | **R** | Currently accepted but **not used** — server reads `pending_qty_kg` from DB and ignores this override |
| `so_number` | string | O-null | |
| `custom_qty_units` | int | O-null | **Whole units only** (was float, changed 2026-05-07). Pydantic rejects floats |
| `uom` | number | O-null | kg per unit (e.g. `0.25` for a 250g pack) |
| `bom_overrides` | array | O | Default `[]`, currently passthrough |
| `floors` | string[] | O | Allowed floors for this item |
| `machines` | dict<string, string[]> | O | `{floor: [machine_names]}` — machines allowed per floor |
| `floor_qty` | dict<string, number> | O | `{floor: kg}` — per-floor kg split. Sum should ≈ `custom_qty_kg` |
| `floor_qty_units` | dict<string, int> | O | **NEW.** `{floor: units}` — per-floor unit split. **Whole ints**. Sum should ≈ `custom_qty_units`. AI is told not to recompute units from kg/uom when this is supplied |

**Constraint group rules:**
- If `floors` is empty AND `machines` is empty → no constraint sent to AI; AI assigns machines freely.
- If either is non-empty → both are forwarded as `user_constraints` for that item; AI must obey.
- `keys(floor_qty)` and `keys(floor_qty_units)` should both be subsets of `floors`. Backend does NOT validate this — invalid floor names will be silently passed to the AI which may ignore them.

**Example:**
```json
{
  "entity": "cfpl",
  "plan_type": "daily",
  "plan_date": "2026-05-08",
  "plan_name": "",
  "created_by": "alice",
  "selected_items": [
    {
      "fulfillment_id": 42,
      "custom_qty_kg": 500.0,
      "custom_qty_units": 2000,
      "so_number": "SO/26-27/0123",
      "uom": 0.25,
      "bom_overrides": [],
      "floors": ["Floor 1", "Floor 2"],
      "machines": {
        "Floor 1": ["Roaster A", "Packer 2"],
        "Floor 2": ["Roaster B"]
      },
      "floor_qty":       { "Floor 1": 300.0, "Floor 2": 200.0 },
      "floor_qty_units": { "Floor 1": 1200,  "Floor 2": 800 }
    }
  ]
}
```

**Response** `200`:

```json
{
  "plan_id": 137,
  "status": "draft",
  "lines": 4,
  "material_check": [
    { "material": "Almonds Raw", "type": "rm",
      "needed_kg": 525, "available_kg": 2000, "status": "SUFFICIENT" }
  ],
  "risk_flags": [
    { "flag": "Tight deadline on SO/26-27/0123",
      "severity": "warning", "details": "..." }
  ],
  "no_bom_items": [],
  "plan_name": "Daily Plan — 2026-05-08",
  "schedule": [
    {
      "fg_sku_name": "Almonds Roasted 250g",
      "customer_name": "Reliance",
      "qty_kg": 300,
      "qty_units": 1200,
      "bom_id": 15,
      "production_type": "production",
      "machine_name": "Roaster A",
      "floor": "Floor 1",
      "priority": 1,
      "shift": "day",
      "stage_sequence": ["sorting", "roasting", "packaging"],
      "estimated_hours": 5.0,
      "linked_fulfillment_ids": [42],
      "reasoning": "Earliest deadline, machine available"
    }
  ]
}
```

**Nullability of response root:**

| Field | Nullable? |
|---|---|
| `plan_id`, `status`, `lines`, `plan_name` | NEVER NULL |
| `material_check`, `risk_flags`, `no_bom_items`, `schedule` | NEVER NULL (default `[]`) |

**Nullability inside `schedule[]`:**

| Field | Nullable? |
|---|---|
| `fg_sku_name`, `qty_kg`, `priority`, `shift` | NEVER NULL |
| `customer_name` | MAY be null (falls back to fulfillment lookup; can still resolve to null) |
| `qty_units`, `bom_id`, `production_type`, `machine_name`, `floor`, `stage_sequence`, `estimated_hours`, `reasoning`, `linked_fulfillment_ids` | MAY be null/empty (AI-emitted) |

**Nullability inside `material_check[]`:** all fields non-null when present.
**Nullability inside `risk_flags[]`:** `flag` and `severity` non-null; `details` may be null.

**Errors:**
- `400` — empty `selected_items`
- `200 with `lines: 0`` — Claude returned malformed JSON. Look for `risk_flags[].severity == "error"` with flag `"AI response parse error"`. **This is not a 5xx — frontend must check.**
- `422` — Pydantic validation failure. Most likely: `custom_qty_units` or `floor_qty_units` value is a float, e.g. `2000.5`

### Frontend prompt — Section 1

> Build a "Create AI Plan" screen. Step 1: list of fulfillment items (paginated, filtered by entity/SO/customer). User multi-selects items. Step 2: per selected item, a constraint editor — floor multi-select, machine multi-select per chosen floor, and a per-floor split table with TWO columns: `kg` and `units`. The kg column sums to `custom_qty_kg` (auto-balanced), the units column sums to `custom_qty_units` (auto-balanced as integers — when user edits one floor's units, redistribute remainder across other floors as ints). Reject non-integer units inputs at form level — the API will 422 on floats. Step 3: review screen. Step 4: POST `/plans/create-with-ai`. On success, navigate to plan-detail with `plan_id`. **Handle the soft-failure case**: if response has `lines: 0` AND a `risk_flags[]` entry with severity `"error"` and flag containing `"parse error"`, show "AI generation failed — please retry" instead of treating the empty plan as success.
>
> The constraint payload assembly: build `floor_qty` and `floor_qty_units` dicts only for floors that the user actually edited. If user only chose floors but didn't split, omit both dicts (server will tell AI to split equally). Do not send dicts with empty values.

---

## Section 2 — Plan Revision
*Activity:* `PlanRevisionActivity` — invoke when a change event hits an existing plan

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/plans/{plan_id}/revise-with-ai` | **NEW.** Generate a revised plan via Claude AI |

### `POST /plans/{plan_id}/revise-with-ai`

**Path params:** `plan_id: int`
**Permission required:** `production/plans/revise/create`. The endpoint is gated by `require_permission(...)` — calls without a valid JWT or without the permission return `403`.

**Request body:**

| Field | Type | Required? | Notes |
|---|---|---|---|
| `change_event` | string | **R** | Free-text description of the trigger. Whitespace-only is rejected. Min effective length 1 char after `.strip()` |

> **Note:** `entity` is NOT accepted — the backend always derives entity from the source plan to prevent cross-entity revisions.

```json
{
  "change_event": "Roaster A breakdown — needs reassign to Roaster B from 14:00"
}
```

**Response** `200`:

```json
{
  "old_plan_id": 137,
  "new_plan_id": 142,
  "status": "draft",
  "revision_number": 2,
  "lines_kept": 6,
  "lines_added": 1,
  "lines_cancelled": 1,
  "material_check": [
    { "material": "Almonds Raw", "type": "rm",
      "needed_kg": 300, "available_kg": 500, "status": "SUFFICIENT" }
  ],
  "risk_flags": [
    { "flag": "Roaster B has back-to-back batches; tight changeover",
      "severity": "warning", "details": "..." }
  ],
  "revised_schedule": [
    { "action": "keep",       "plan_line_id": 1,
      "reasoning": "Already in_progress" },
    { "action": "reschedule", "plan_line_id": 2,
      "new_priority": 3, "new_machine_name": "Roaster B", "new_shift": "night",
      "floor": "Floor 1",
      "reasoning": "Moved to Roaster B due to breakdown" },
    { "action": "cancel",     "plan_line_id": 3,
      "reasoning": "RM no longer available" },
    { "action": "add",
      "fg_sku_name": "Cashews Salted 200g", "customer_name": "DMart",
      "qty_kg": 100, "qty_units": 500, "bom_id": 22,
      "machine_name": "Packer 1", "floor": "Floor 2",
      "priority": 1, "shift": "day",
      "stage_sequence": ["packaging"], "estimated_hours": 2.0,
      "linked_fulfillment_ids": [99],
      "reasoning": "Emergency reorder slotted in" }
  ]
}
```

**Nullability of response root:**

| Field | Nullable? |
|---|---|
| `old_plan_id`, `new_plan_id`, `status`, `revision_number`, `lines_kept`, `lines_added`, `lines_cancelled` | NEVER NULL |
| `material_check`, `risk_flags`, `revised_schedule` | NEVER NULL (default `[]`) |

**Nullability inside `revised_schedule[]`:**

| Field | Nullable? | Applies to action |
|---|---|---|
| `action` | NEVER NULL | all |
| `plan_line_id` | NEVER NULL | `keep`, `reschedule`, `cancel` |
| `reasoning` | NEVER NULL | all (server defaults to `"Rescheduled"` / `"Added in revision"`) |
| `new_priority`, `new_machine_name`, `new_shift`, `floor` | MAY be null | `reschedule` (each falls back to old line value if null/missing) |
| `fg_sku_name`, `qty_kg`, `priority`, `shift` | NEVER NULL | `add` |
| `customer_name`, `qty_units`, `bom_id`, `machine_name`, `floor`, `stage_sequence`, `estimated_hours`, `linked_fulfillment_ids` | MAY be null | `add` |

**Errors:**

| Status | Cause | What frontend should do |
|---|---|---|
| `400` | `change_event` empty or whitespace-only | Inline form error: "Describe the change before submitting" |
| `403` | Missing JWT / permission `revise_plan` not granted to user's role / user's `allowed_entities` doesn't include the plan's entity | Toast: "You don't have permission to revise plans for this entity." Hide button if you can pre-check role. |
| `404` | `plan_id` doesn't exist | Toast + navigate back to plan list |
| `409` | Plan status not in `{draft, approved, executed}` (terminal `cancelled` or already-superseded `revised` cannot be revised). Detail string includes the actual status AND, when available, `"Latest revisable plan in this chain is plan_id=N"` | Parse the detail. If `plan_id=N` is mentioned, offer a "Open plan N" link in the toast |
| `409` (race) | Plan status changed during the multi-second Claude call | Toast: "Plan was modified by another user — please reload and try again" |
| `200` (soft-fail) | Empty `revised_schedule` and a `risk_flags[]` entry with severity `"error"` | Same handling as create endpoint's soft-fail |

### Frontend prompt — Section 2

> Build a "Revise Plan" dialog accessible from the plan-detail screen. Trigger button visible **only** when the plan's status ∈ `{draft, approved, executed}`. The dialog has one required field: `change_event` (multiline text, min 1 non-whitespace char). On submit, POST `/plans/{plan_id}/revise-with-ai`, show a "Generating revision..." spinner (the call can take 10-30s due to Claude latency). On 200, navigate to the new plan-detail at `new_plan_id`. Render `revised_schedule[]` as a diff timeline with color-coded action chips: blue (keep), orange (reschedule), red (cancel), green (add). For `reschedule` actions, show before/after for machine/priority/shift fields by joining against the old plan lines (re-fetch old plan if needed). For `add` actions, render the full new line. Show `lines_kept | lines_added | lines_cancelled` as a top-bar summary.
>
> **Race-condition handling:** if 409 with detail containing `"changed to"`, show "Plan was modified — reload required" with a "Reload plan" button. If 409 mentions `"Latest revisable plan in this chain"`, parse out the `plan_id=N` and offer a "Switch to plan N" navigation button.
>
> **Permission pre-check:** if your auth state knows the user's role lacks `production/plans/revise/create`, hide the Revise button entirely (don't rely on the 403 round-trip).

---

## Section 3 — Plan Read Endpoints
*Activity:* `PlanListActivity`, `PlanDetailActivity`

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/plans` | Paginated list with filters |
| GET | `/plans/all` | Same filters, no pagination |
| GET | `/plans/{plan_id}` | Plan detail with lines + analysis |

### `GET /plans`

**Query params** (all optional):

| Param | Type | Notes |
|---|---|---|
| `entity` | string | `cfpl` / `cdpl` |
| `status` | string | Exact match. Domain: `draft, approved, executed, cancelled, revised` |
| `plan_type` | string | `daily` / `weekly` |
| `date_from` | string (YYYY-MM-DD) | Filter on `plan_date >=` |
| `date_to` | string (YYYY-MM-DD) | Filter on `plan_date <=` |
| `page` | int | Default 1, ≥1 |
| `page_size` | int | Default 200, max 500 |

**Response** `200`:

```json
{
  "results": [
    {
      "plan_id": 137,
      "plan_name": "Daily Plan — 2026-05-08",
      "entity": "cfpl",
      "plan_type": "daily",
      "plan_date": "2026-05-08",
      "date_from": "2026-05-08",
      "date_to":   "2026-05-08",
      "status": "draft",
      "ai_generated": true,
      "revision_number": 1,
      "approved_by": null,
      "approved_at": null,
      "created_at": "2026-05-07T14:30:00+00:00"
    }
  ],
  "pagination": { "page": 1, "page_size": 200, "total": 12, "total_pages": 1 }
}
```

**Nullability of result rows:**

| Field | Nullable? |
|---|---|
| `plan_id`, `plan_name`, `entity`, `plan_type`, `plan_date`, `status`, `created_at` | NEVER NULL |
| `ai_generated` | NEVER NULL (boolean) |
| `date_from`, `date_to`, `revision_number` | MAY be null (very old rows) |
| `approved_by`, `approved_at` | NULL until approved |

**Errors:** `400` if `date_from` / `date_to` not parseable as `YYYY-MM-DD`.

### `GET /plans/all`

Same query params as `/plans` except no `page`/`page_size`. Returns the bare results array (no `pagination` key). Use sparingly — heavy.

```json
[ { "plan_id": 137, "...": "..." } ]
```

### `GET /plans/{plan_id}`

**Path params:** `plan_id: int`
**Response** `200`:

```json
{
  "plan_id": 137,
  "plan_name": "Daily Plan — 2026-05-08",
  "entity": "cfpl",
  "plan_type": "daily",
  "plan_date": "2026-05-08",
  "date_from": "2026-05-08",
  "date_to":   "2026-05-08",
  "status": "draft",
  "ai_generated": true,
  "ai_analysis_json": { "/* full Claude response — schedule, material_check, risk_flags */": null },
  "revision_number": 1,
  "previous_plan_id": null,
  "approved_by": null,
  "approved_at": null,
  "created_at": "2026-05-07T14:30:00+00:00",

  "lines": [
    {
      "plan_line_id": 401,
      "plan_id": 137,
      "fg_sku_name": "Almonds Roasted 250g",
      "customer_name": "Reliance",
      "bom_id": 15,
      "planned_qty_kg": 300.0,
      "planned_qty_units": 1200,
      "machine_id": 7,
      "priority": 1,
      "shift": "day",
      "stage_sequence": ["sorting", "roasting", "packaging"],
      "estimated_hours": 5.0,
      "linked_so_fulfillment_ids": [42],
      "reasoning": "Earliest deadline, machine available",
      "floor": "Floor 1",
      "status": "planned"
    }
  ],

  "material_check": [],
  "risk_flags":      []
}
```

**Nullability inside `lines[]`:**

| Field | Nullable? |
|---|---|
| `plan_line_id`, `plan_id`, `fg_sku_name`, `planned_qty_kg`, `priority`, `shift`, `status` | NEVER NULL |
| `customer_name`, `bom_id`, `planned_qty_units`, `machine_id`, `stage_sequence`, `estimated_hours`, `linked_so_fulfillment_ids`, `reasoning`, `floor` | MAY be null |

**Nullability of root:**

| Field | Nullable? |
|---|---|
| `plan_id`, `plan_name`, `entity`, `plan_type`, `plan_date`, `status`, `ai_generated`, `created_at` | NEVER NULL |
| `ai_analysis_json` | MAY be null (manually-created plans) |
| `previous_plan_id` | NULL for original (non-revision) plans |
| `revision_number` | NULL for very old rows; `1` for originals; `>1` for revisions |
| `approved_by`, `approved_at`, `date_from`, `date_to` | MAY be null |
| `material_check`, `risk_flags`, `lines` | NEVER NULL (default `[]`) |

**Errors:** `404` if not found.

### Frontend prompt — Section 3

> Build a "Plans" list screen using GET `/plans`. Filters: entity, status (single-select chip group with the 5 status values), plan_type (daily/weekly), date_from/date_to. Columns: plan_name, plan_date, status (color chip — see Cross-cutting), revision_number (badge — show "REV N" if > 1), ai_generated (sparkle icon), created_at. Tap row → plan-detail.
>
> Plan-detail screen renders `lines[]` as the schedule table (sortable by `priority`, grouped by `floor` if non-null), `material_check[]` as a collapsible list with status chips (`SUFFICIENT`/`SHORT`), `risk_flags[]` as a banner area at the top (severity-color-coded). The `previous_plan_id` field renders as "Revision of plan #N" link. Action buttons:
> - **Revise** (Section 2) — visible when `status ∈ {draft, approved, executed}`
> - **Approve** / **Cancel** etc — out of scope for this spec
>
> When `revision_number > 1`, show a small "Revision history" link that walks the chain (no single endpoint exists today — see Cross-cutting "Known gaps").

---

## Section 4 — Claude-Specific Behavior

The schedule and revision content come from **Anthropic's Claude API** (`claude-opus-X` per `settings.CLAUDE_MODEL`). This is not a deterministic planner — same inputs can produce different outputs, and outputs are best-effort suggestions, not authoritative facts. Frontend must treat AI fields as **proposals subject to user review**.

### 4.1 Latency

| Endpoint | Typical latency | P99 |
|---|---|---|
| `POST /plans/create-with-ai` | 8–20 s | 45 s |
| `POST /plans/{id}/revise-with-ai` | 10–25 s | 60 s |

Claude responses are not streamed back to the client. The whole call blocks until Claude returns, then the DB writes happen in the same request. Keep the dialog open with an indeterminate progress bar; do **not** show a quick spinner.

UX rules:
- Show elapsed-seconds counter; if > 30 s, change copy to "Still working — Claude is thinking through your constraints."
- Do not auto-cancel client-side. The backend has no way to roll back a partially-charged Claude call, so a user-initiated cancel should warn: "If Claude already responded, the plan will be created anyway — you'll see it in the list."
- Disable the submit button for the duration. Repeat clicks generate **separate** Claude calls (and separate plans).

### 4.2 Non-determinism

The same `selected_items` posted twice will produce two different `schedule[]` arrays. Implications:
- Don't show a "Regenerate" button next to a "Create" button without warning the user that it costs another Claude call and produces a different result.
- The `revision_number` field reflects DB lineage, not "version of the AI's thinking." If a user creates plan A and plan B from identical inputs, they're independent — neither is `previous_plan_id` of the other.
- Caching is not safe. Don't dedupe identical requests on the client.

### 4.3 Hallucination risk

Claude is told the available BOMs, machines, fulfillments, and constraints in the prompt context (see `ai_planner.collect_planning_context`). It is **strongly told** to honour them, but does not always. Specifically:

| Field | What can go wrong | Frontend defense |
|---|---|---|
| `fg_sku_name` in schedule | Should match the fulfillment's `fg_sku_name`. Usually does. | Render but don't blindly trust — join against fulfillment row before showing in user-editable form |
| `customer_name` | Pulled from DB if missing, but may still be blank. The backend does this fallback in `create_plan_from_ai`. | Render `—` when null |
| `machine_name` | Should be in `allowed_machines` for the item. Claude may pick a different one. | Highlight in red if `machine_name` is not in the user's `allowed_machines[floor]` list. Backend does NOT validate this. |
| `floor` | Should be in `floors[]` for the item. | Same — highlight mismatches. |
| `qty_kg` per floor | Should match `floor_qty[floor]` when supplied. | Diff against what the user submitted; flag any line where `Math.abs(claude_qty - user_qty) > 0.5`. |
| `qty_units` per floor | Should match `floor_qty_units[floor]` when supplied. | Same — but also `Math.round()` defensively in case Claude emits a float. |
| `bom_id` | Should match the fulfillment's BOM. Backend re-resolves via `fg_sku_name` if missing. | Don't trust client-side; treat as a hint. |
| `linked_fulfillment_ids` | Should reference `fulfillment_id`s the user actually selected. Claude can hallucinate IDs. | Filter to the intersection with `selected_items[].fulfillment_id` before rendering. |
| `stage_sequence` | Should match the BOM's process route. May be re-ordered or invented. | Treat as a hint; render but don't block on it. |
| `estimated_hours` | Pure estimate; sometimes wildly off | Render as "~Xh" not "Xh exactly." |
| `reasoning` | Free text. Sometimes very useful, sometimes generic. | Always show — this is Claude's only way to explain itself to the user. Truncate to ~140 chars in the table; expand on row click. |

**The frontend MUST validate constraints client-side and surface mismatches before the user approves the plan.** A "Constraints honored?" check column on the schedule table is recommended. Backend writes whatever Claude returns; the only line of defense is the user reviewing the draft.

### 4.4 The `risk_flags[]` array

This is **Claude's editorial commentary**, not a structured warning system. Severity values seen in the wild: `info`, `warning`, `error`. The `error` severity is reserved for one specific case the backend injects:

```json
{ "flag": "AI response parse error", "severity": "error",
  "details": "<first 500 chars of Claude's malformed text>" }
```

When you see this, the rest of the response is empty (`lines: 0`, `schedule: []`). Don't try to render it as a normal plan.

For non-error severities, render the flags as a banner above the schedule. Don't summarize or filter them — Claude expects them to be read verbatim by the planner.

### 4.5 The `reasoning` field on schedule lines

Per-line free text from Claude. Examples seen:
- *"Earliest deadline (2026-05-10), sufficient RM stock, machine free 09:00–14:00"* — useful
- *"Scheduled this way"* — useless

Treat as untrusted text. Render with `whiteSpace: pre-wrap` so newlines from Claude are preserved. Do NOT parse it for fields — there is no machine-readable structure inside `reasoning`.

### 4.6 Revision-specific Claude behavior

`revise-with-ai` sends the *current* plan state to Claude, including the in-progress / completed line statuses. Rules Claude is told:

- Lines `in_progress` or `completed` MUST be emitted as `action: keep` with no changes.
- Planned lines MAY be `keep`, `reschedule`, `cancel`, or new `add` lines may be inserted.

In practice Claude usually obeys this. **But:** if Claude emits `reschedule` on a line that was actually `in_progress`, the backend will copy it to the new plan with the new machine/priority anyway — there's no server-side check. Frontend defense:
- Before showing the revised schedule, fetch the OLD plan's lines.
- For each `reschedule` action, look up the old line's `status`. If it was `in_progress` or `completed` and Claude is rescheduling it, mark with a warning chip: "Claude tried to reschedule a line that's already running — review carefully."

### 4.7 Token / cost considerations

Each call costs Anthropic tokens. The backend logs `tokens_used` and `latency_ms` to `ai_recommendation` but does NOT return them to the client today. If you want to surface token usage:
- Add an `Authorization`-gated GET `/plans/{id}/ai-recommendation` (does not exist today — would need a backend addition).
- For now, treat this as opaque server-side cost.

Don't expose a "free retry" button — every retry costs real money. Confirmation dialog before re-running.

### 4.8 What Claude does NOT see

The prompt context excludes:
- Per-machine current load (only capacity_kg_per_hr, not a calendar)
- Active job cards from OTHER plans
- Real-time machine status (breakdown, maintenance window) — except whatever the user types into `change_event` for revisions
- Customer SLA / penalty data
- Operator availability

If your UI surfaces any of these to the user, surface them again *next to* the AI plan so the planner can override Claude's suggestions.

### Frontend prompt — Section 4

> Wherever an AI-generated field is rendered in the UI, mark it with a small "AI" badge (or color the text). Provide a constraints-validation banner at the top of every plan-detail screen that runs client-side checks: do all `machine_name` values appear in the constraint payload? Do `floor_qty` totals add up? Are linked fulfillments in the user's selection? Show pass/warn/fail for each. The user's approval workflow should not let them approve a plan with unresolved constraint warnings without an explicit "I've reviewed Claude's suggestions and accept the deviations" checkbox. Persist this acknowledgement to your audit trail (frontend-only is fine; backend doesn't track it).

---

## Cross-cutting concerns

### Status colors (suggested palette)

| Plan status | Color | Meaning |
|---|---|---|
| `draft` | grey | AI-generated, not yet approved |
| `approved` | blue | Approved by planner; production may proceed |
| `executed` | green | Job cards generated and in flight |
| `revised` | orange (with strikethrough) | Superseded by a newer revision; read-only |
| `cancelled` | red | Terminal; not revisable |

### Schedule line status (per plan_line)

| Line status | Color |
|---|---|
| `planned` | grey |
| `in_progress` | green |
| `completed` | dark green |
| `cancelled` | red |

### When to refetch
- After POST `/plans/create-with-ai` → navigate to detail; fetch fresh
- After POST `/plans/{id}/revise-with-ai` → navigate to **`new_plan_id`** detail; the old plan is now `status='revised'` — refresh list views
- WebSocket events `plan.created` / `plan.revised` / `plan.approved` carry `plan_id`; refetch the detail if you're showing it

### Common error handling

| Status | What to show |
|---|---|
| `401` | Refresh token, retry once. If still 401, redirect to login |
| `403` | Toast with `detail`. Hide the action button if you can pre-check the role |
| `404` | "Plan not found — it may have been deleted" + navigate back |
| `409` | Show response `detail` verbatim — these messages are designed to be user-readable. For the revise endpoint, parse `plan_id=N` if present and offer navigation |
| `422` | Inline validation. Pydantic emits `{detail: [{loc: [...], msg: "...", type: "..."}, ...]}`. Map `loc[1]` (or deeper) to your form field |
| `500` | Generic toast + log |

### Soft-failure pattern (CRITICAL)

Both `create-with-ai` and `revise-with-ai` return `200 OK` with an empty schedule + a synthetic risk flag when Claude returns malformed JSON. **Always check after a successful response:**

```javascript
const isAIParseFailure = (response) =>
  (response.lines === 0 || response.revised_schedule?.length === 0)
  && response.risk_flags?.some(f => f.severity === 'error'
                                    && f.flag.includes('parse error'));
```

When true, show a retry dialog — do NOT treat as a successful empty plan.

### Field-level conventions

- All `*_kg` fields are floats; all `*_units` fields are **integers** (since 2026-05-07 — was previously float on `custom_qty_units` and `floor_qty_units`).
- The `floor` field on a schedule line / plan line is a free-text string matching the user's `floors[]` selection at creation time. Match case-insensitively when joining against machine/floor data.
- `linked_fulfillment_ids` / `linked_so_fulfillment_ids` is `int[]` — the backend coerces strings to ints in the revise path, but the create path does not. Always send ints.

### Known backend gaps (relevant to the frontend)

1. **`create-with-ai` is unprotected.** No JWT or permission check. If your build target enforces auth, the frontend should still send a Bearer token; the backend will accept it but won't validate. (`revise-with-ai` IS gated correctly.)
2. **`custom_qty_kg` is ignored.** Backend reads `pending_qty_kg` from DB. If you let users edit the kg field, warn them it's display-only.
3. **No filter on `previous_plan_id` in `/plans`.** Walking the revision chain from the list endpoint requires multiple round-trips. The 409 detail from `revise-with-ai` is the only hint about chain heads.
4. **The MCP planner path emits `qty_units` as integers but the HTTP create response may include floats** if Claude produced a fractional value. Frontend should `Math.round()` defensively when displaying units.

### Permission map (for pre-check / button gating)

| Endpoint | Required permission tuple `(module, sub_module, sub_sub_module, action)` |
|---|---|
| `POST /plans/create-with-ai` | currently none (gap) — should be `(production, plans, NULL, create)` |
| `POST /plans/{id}/revise-with-ai` | `(production, plans, revise, create)` — enforced; entity-scoped via plan's entity |
| `GET /plans*` | not enforced today |

For the revise endpoint, the backend re-checks the user's `allowed_entities` against `plan.entity` after fetch — so a user with `revise_plan` for entity `cfpl` cannot revise a `cdpl` plan even if they crafted the request.
