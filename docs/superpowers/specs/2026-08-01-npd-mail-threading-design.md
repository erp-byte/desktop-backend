# NPD mail threading — one trail per transaction (design)

Date: 2026-08-01
Scope: **server_replica only.** No DB migration, no frontend change.

## Problem

One NPD transaction sprays four separate mail trails, and the promote decision mails nothing at all.

| Event | Function | Subject | Thread root |
|---|---|---|---|
| Requisition created | `notify_inventory_informative("created")` | `Sample Request {rid} — Inventory Notice` | `inv` scope |
| Submitted (Accept/Hold) | `notify_npd_review_email` | `NPD Sample Request {rid}` | `npd` scope |
| Accepted / held | `notify_requestor_email` + `notify_inventory_informative` | both of the above | 2 trails |
| Promote requested | `notify_promote_review_email` | `NPD Promote Approval — Dev JC {id}` | `promote-inv`, `promote-bh` |
| Promote decided | — | — | **nothing sent** |

The four `_anchor_msgid(..., scope=)` values *are* the four trails. WhatsApp promote
approvals go through `act_promote_approval`, which never mailed — hence "the status
update is not mailed under the approval mail".

Customer Returns already solved this (`customer_returns/services/mail_service.py`):
one deterministic `_thread_key(cr_id)`, one constant subject, buttoned copy to the
approver alone plus a button-less copy threaded under it. This ports that pattern.

## Decisions (locked with the user)

- **Trail scope**: one common To/Cc for the whole transaction — everyone on the
  transaction sees the full trail, not just the events addressed to their role.
- **Approver**: gate holders only, one mail each, addressed to them alone. No action
  URL ever appears in a mail with more than one recipient.
- **Promote link**: a dev JC with `source_requisition_id` threads into that
  requisition's trail. A standalone dev JC is its own transaction and roots its own.
- **DC copy**: a **link** to the existing `/job-cards/{id}/gate-pass` print page, not a
  PDF attachment. The dev-JC Delivery Challan is a client-only React print page with no
  backend generator; attaching it would mean a new server-side FPDF renderer to keep in
  sync with the React page.
- **DC trigger**: on dev-JC dispatch (`dispatch_dev_sample`), where the DC becomes real.

## Design

### 1. One thread key per transaction

Pure function of the id, no DB column — CR's rule. (`sample_requisitions.email_thread_msgid`
is already a dead column and stays dead.)

```python
def _thread_key(request_id):   return f"<NPD-{request_id}@candorfoods.in>"
def _jc_thread_key(dev_jc_id): return f"<NPD-JC-{dev_jc_id}@candorfoods.in>"
```

Retires `_anchor_msgid()` — its per-recipient SHA1 and all four `scope=` variants.

### 2. One constant subject

`NPD Sample Request {request_id}` on every mail of the transaction: created, review,
accept, hold, hold re-offer, reminder, promote request, promote decision, dispatch.
Gmail breaks a conversation the moment the subject changes, so status lives in the body.
A standalone dev JC uses `NPD Dev Job Card {dev_jc_id}`.

Retires `Sample Request {rid} — Inventory Notice` and `NPD Promote Approval — Dev JC {id}`.

### 3. Root vs reply

- **Root** — the requisition-created mail, `Message-ID: <NPD-{request_id}@candorfoods.in>`.
- **Every later mail** — fresh `Message-ID`, `In-Reply-To` + `References` = thread key.
- A standalone dev JC roots on its own key at the promote-request broadcast.

### 4. One recipient set per transaction

`resolve_recipients(conn, req)`, ported from CR's function of the same name but resolved
from roles + the requisition row (NPD has no `cr_email_routing` equivalent and this
introduces none):

- **To** — the requestor (`requestor_user_id` → `auth_user.email`); falls back to the
  `npd_team` pool when it does not resolve, so a mail is never addressed to nobody.
- **Cc** — `npd_team` + `inventory_manager`, deduped, minus whatever is in To.

### 5. Buttons go to the gate holder alone

Per buttoned event, `notify_cr_created`'s exact split:

1. **Buttoned copy** — one mail per gate holder, `To:` them alone, **no Cc**, carrying
   their own signed link. Threaded reply.
2. **Button-less copy** — identical card, buttons stripped, to To/Cc **minus every gate
   holder** who just received a buttoned copy.

| Event | Buttons to |
|---|---|
| Submit / hold re-offer / reminder | each `npd_team` reviewer (Accept / Hold) |
| Promote — `INV_MGR` gate | each `inventory_manager` (Approve / Reject) |
| Promote — `REQUESTOR_BH` gate | the requisition's `requestor_user_id` (Approve / Reject) |

Reminders send the buttoned copy only — no broadcast, so the whole list is not re-spammed
every 24h.

### 6. Promote decision status update (new)

`notify_promote_status_email()` — button-less threaded reply fired from
`act_promote_approval` **after** the transaction commits. That is the single choke point
all three paths funnel through — WhatsApp `_apply_promote`, the email button
`POST /email/promote-action`, and the in-app `POST /npd-dev-job-cards/{id}/promote-approval`
— so a WhatsApp approval mails a status update by construction.

Fires on **every** gate decision, both gates, accept and reject. Body reports gate, actor
and outcome:

- `REJECTED` — gate + actor + reason; promote voided
- `PENDING_APPROVAL` — gate cleared by actor, N gate(s) remaining
- `PROMOTED` — both gates clear, recipe live

Requires one small refactor: `act_promote_approval` currently `return`s from inside
`async with conn.transaction()` on the REJECT branch. Assign `result` and send after the
block — behaviour-identical, keeps SMTP off the transaction.

### 7. Dispatch / DC closing mail (new)

`notify_dev_dispatch_email()` — button-less threaded reply fired from
`dispatch_dev_sample` after commit, closing the trail with the dispatch summary (qty, uom,
recipient, outpass sub-number) and a link to
`{WEB_APP_URL}/modules/npd-development/job-cards/{id}/gate-pass?dispatch={dispatch_id}`.

## Consequences

- **Volume up.** Common To/Cc means inventory managers now receive the review and promote
  cards (button-less), and npd_team receives the requisition-created notice. That is the
  cost of "everyone sees the full trail", accepted by the user.
- **In-flight requisitions break once.** Anything mid-flow carries old anchors; its next
  mail starts the new unified trail. No data migration, one-time cosmetic split.
- **The DC link is role-gated.** `/job-cards/{id}/gate-pass` is npd_team + admin only
  (`sampleCaps(me).canOutpass`); BH and inventory managers are redirected away. The mail
  body therefore carries the full dispatch summary in text so it stands alone. Relaxing
  that gate is a deliberate access-control change and is **out of scope** — flagged for
  the user to decide separately.

## Out of scope

- The dev-JC DC as a PDF attachment (would need a new server-side FPDF renderer).
- Relaxing `canOutpass` on the gate-pass page.
- The WhatsApp templates; NPD accept/hold over WhatsApp already mails correctly via
  `act_npd_review`.
- `frontend_replica` (Electron).

## Verification

- `python -m compileall` on every changed module; import the package to catch bad
  references.
- Grep that no caller still references the removed `_anchor_msgid` /
  `notify_inventory_informative` / `threaded=` kwarg.
- UAT: raise an NPD requisition, submit, accept, request promote, approve one gate over
  WhatsApp, approve the second, dispatch — confirm a single Gmail conversation containing
  every step, and that action buttons appear only in the single-recipient copies.
