# Sample Issuing Module — Frontend API

> Base path: `/api/v1/sample` · Auth: `Authorization: Bearer <access-token>` on every call.
> Errors use the standard envelope: `{ "error": "<code>", "message": "...", "details": {...} }`.
> Illegal status transitions return **409**; validation failures **422**; missing records **404**;
> permission failures **403**. Schema: migrations `app/db/samples/035–038`.

## Status flow (spec §8)

```
DRAFT ─submit→ SUBMITTED ─approve→ BH_APPROVED ─reject→ BH_REJECTED ─edit→ SUBMITTED
BH_APPROVED ─(RM/INTERNAL outward)→ READY_FOR_DISPATCH
BH_APPROVED ─(FG/NPD start-production)→ IN_PRODUCTION ─mark-packing→ PACKING ─mark-ready→ READY_FOR_DISPATCH
READY_FOR_DISPATCH ─issue-gate-pass→ GATE_PASS_ISSUED ─(auto/close)→ CLOSED
READY_FOR_DISPATCH ─dispatch-internal→ INTERNALLY_DISPATCHED
INTERNALLY_DISPATCHED ─convert-full→ GATE_PASS_ISSUED
INTERNALLY_DISPATCHED ─convert-partial→ PARTIALLY_CONVERTED (+ child → GATE_PASS_ISSUED)
* ─cancel→ CANCELLED
```

## Roles (permission gate per endpoint)

| Action | Required permission | Default roles |
|---|---|---|
| view | `sample/view` | all (viewer+) |
| create/edit requisition, submit | `sample/create`, `sample/edit` | planner, business_head, npd_team, admin |
| BH approve/reject, BH sign-off (086) | `sample/approve/create` | business_head, admin |
| production ack (start/packing) | `sample/production_ack/create` | floor_manager, admin |
| outward, dispatch, inv-verify, mark-ready | `sample/inv_signoff/create` | inventory_manager, admin |
| NPD draft author / promote | `sample/npd/create`, `sample/npd/promote/create` | npd_team, admin |
| issue/print/void gate pass | `sample/gate_pass/create` | inventory_manager, admin |
| convert internal→external | `sample/convert/create` | business_head, admin |

## Endpoints

### Requisitions
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/requisitions` | `RequisitionCreate` | creates DRAFT + article lines |
| GET | `/requisitions?status=&sample_type=&entity=&limit=&offset=` | — | list (newest first) |
| GET | `/requisitions/{id}` | — | detail: `{...req, articles[], approvals[], audit[]}`. `bh_signoff_state` (086) is `PENDING\|APPROVED\|AUTO_APPROVED\|REJECTED\|NOT_REQUIRED`, or absent on pre-086 rows. |
| PATCH | `/requisitions/{id}` | `RequisitionUpdate` | DRAFT / BH_REJECTED only |
| POST | `/requisitions/{id}/submit` | — | guards: ≥1 article, valid sku_id, qty>0 |
| POST | `/requisitions/{id}/cancel` | `{reason}` | any non-terminal status |
| POST | `/requisitions/{id}/approve` | `{action: APPROVED\|REJECTED, remarks?}` | reject requires remarks; non-NPD flow |
| POST | `/requisitions/{id}/bh-signoff` | `{action: APPROVED\|REJECTED, remarks?}` | **086** — the NPD/TRIAL business-head gate. Only actionable while `bh_signoff_state = PENDING`, and only by the BOUND `business_head_user_id` (or an admin). Approve releases the request to the NPD team (status stays SUBMITTED); reject moves it to BH_REJECTED and requires a reason. |

### Business-head approval on an NPD/TRIAL request (086)
The BH approval used to sit on the dev job card's promote (a `REQUESTOR_BH` gate). It now
sits on the REQUEST, and the promote raises only its `INV_MGR` gate.

On submit of an NPD/TRIAL requisition:
- `sales_poc_user_id != business_head_user_id` → `bh_signoff_state = PENDING`. The BH is
  messaged (email card with Approve/Reject + the WhatsApp template); the NPD team is **not**
  told about the request at all until that approval lands.
- otherwise (the BH raised it themselves, or no BH was named) → `AUTO_APPROVED` /
  `NOT_REQUIRED`, no message, straight to NPD. An `AUTO_APPROVED` gate still writes a
  `REQUESTOR_BH_SIGNOFF` approval row so the audit distinguishes it from "never asked".

`POST /requisitions/{id}/npd-review` returns 409 `awaiting_bh_signoff` while the gate is
PENDING.

Public, email-authenticated endpoints behind the mail buttons (no session):

| Method | Path | Body / query | Notes |
|---|---|---|---|
| GET | `/email/bh-signoff` | `request_id, status=approve, email, t` | renders the POST-confirm page (a link-scanner's GET cannot approve) |
| POST | `/email/bh-signoff` | `request_id, email, t` (form) | the real approve; `t` is the HMAC over `("bh_signoff", request_id, email)` |
| POST | `/email/bh-signoff-reject` | `{request_id, email, remarks}` | reason required; the mail's Reject button routes via the web app's request page (`?bh_reject=<request_id>&email=`) |

### Outward & dispatch (Basis RM / Internal)
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/requisitions/{id}/outward` | `{from_location?, issued?: [{article_id, qty}]}` | BH_APPROVED → READY_FOR_DISPATCH, books movement 265 |
| POST | `/requisitions/{id}/dispatch-internal` | — | READY_FOR_DISPATCH → INTERNALLY_DISPATCHED (INTERNAL only) |

### FG / NPD job cards
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/requisitions/{id}/start-production` | — | BH_APPROVED → IN_PRODUCTION; needs `base_bom_id`; generates sample job cards |
| POST | `/requisitions/{id}/mark-packing` | — | IN_PRODUCTION → PACKING |
| POST | `/requisitions/{id}/mark-ready` | — | PACKING → READY_FOR_DISPATCH |

### NPD draft BOM
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/requisitions/{id}/npd-draft` | `NpdDraftCreate` | NPD only; `clone_from_base` copies base bom_line |
| GET | `/npd-drafts/{draft_id}` | — | draft + lines |
| PUT | `/npd-drafts/{draft_id}/lines` | `{lines: NpdLineIn[]}` | replace lines (DRAFT only) |
| POST | `/npd-drafts/{draft_id}/promote` | — | writes a live `bom_header`/`bom_line`; needs BH-approved requisition |

### Gate pass
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/requisitions/{id}/inv-verify` | `{remarks?}` | records INV_MGR_VERIFICATION |
| POST | `/requisitions/{id}/issue-gate-pass` | `GatePassIssueBody` | READY_FOR_DISPATCH → GATE_PASS_ISSUED |
| GET | `/gate-passes/{gp_id}` | — | gate pass + `sample_details` |
| POST | `/gate-passes/{gp_id}/print` | — | **returns `application/pdf`**; bumps `print_count` |
| POST | `/gate-passes/{gp_id}/void` | `{reason}` | no stock auto-reversal (manual 266) |

### Conversion (internal → external)
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/requisitions/{id}/convert-full` | `ConvertFullBody` | INTERNALLY_DISPATCHED → GATE_PASS_ISSUED (no 2nd deduction) |
| POST | `/requisitions/{id}/convert-partial` | `ConvertPartialBody` (`{qty, ...}`) | parent → PARTIALLY_CONVERTED + child gate pass; `qty ≤ issued` |

## Request bodies

```ts
RequisitionCreate = {
  sample_type: "BASIS_RM" | "BASIS_FG" | "NPD" | "INTERNAL",
  entity?: "cfpl" | "cdpl",            // defaults to caller's entity
  requestor_team?: string,
  purpose_tag?: "CUSTOMER_DISPLAY" | "CUSTOMER_ISSUE" | "TASTING_SENSORY" | "PHYSICAL_PARAMETERS" | "INTERNAL_OTHER",
  purpose_note?: string,
  base_bom_id?: number,                // required before start-production (FG/NPD)
  internal_override?: boolean,
  articles: Article[]
}
Article = {
  sku_id: number,                      // MUST come from GET /api/v1/so/sku-lookup (free-text rejected, 422)
  sku_name: string, required_qty: number /* >0 */, uom: string,
  article_role: "RM" | "FG" | "NPD_INPUT" | "NPD_OUTPUT",
  pack_size_kg?: number, notes?: string
}
NpdLineIn = { sku_id, sku_name, qty>=0, uom, item_type?: "rm"|"pm",
              delta_type?: "UNCHANGED"|"ADDED"|"MODIFIED"|"REMOVED", original_qty?, line_order?, notes? }
GatePassIssueBody = { recipient_name?, recipient_contact?, vehicle_carrier?, driver_name?, from_location? }
ConvertPartialBody = GatePassIssueBody & { qty: number /* >0, ≤ issued */, remarks? }
```

## Flow cheat-sheet (golden paths)

- **Basis RM**: create → submit → approve → outward → inv-verify → issue-gate-pass → print
- **Internal**: create → submit → approve → outward → dispatch-internal → (later) convert-full / convert-partial
- **Basis FG**: create(+base_bom_id) → submit → approve → start-production → mark-packing → mark-ready → inv-verify → issue-gate-pass
- **NPD**: create(+base_bom_id) → npd-draft(clone) → edit lines → submit → approve → start-production → … → issue-gate-pass → (optional) promote draft

> Notifications land in `store_alert` (`target_team` ∈ business/inventory/production/stores/npd). Every mutation
> writes a `sample_audit_log` row, surfaced in the requisition detail `audit[]` array.
