# NPD dev-JC promote gate — WhatsApp approval (design)

Date: 2026-06-15

## Goal
Add a WhatsApp approval flow to the NPD dev-JC **promote dual-approval gate**, mirroring
the existing NPD-review WhatsApp flow (`notify_npd_review` + `handle_inbound`). Today the
promote gate (`promote_approval_service.open_promote_request`) fires the **email**
Approve/Reject + an in-app gate, but **no WhatsApp**. Both approvers should be able to act
straight from WhatsApp.

## Decisions (locked with the user)
- **Full mirror** of the NPD-review inbound flow (tap → act directly; typed-command fallback).
- **Both gates**: every `inventory_manager` (INV_MGR gate) + the source requisition's
  requestor BH (REQUESTOR_BH gate), resolved from `auth_user.phone`. Sourceless dev JC →
  inventory_managers only (no REQUESTOR_BH gate exists).
- **Buttons can't be disabled after a tap** — WhatsApp messages are immutable (no edit/recall
  in the Cloud API). Mitigation: idempotent backend + an immediate confirmation reply, so a
  stale re-tap is a harmless "already actioned" no-op. (Standard WhatsApp pattern; same as the
  existing Accept/Hold flow.)
- **Meta template** `npd_promote_approval` already created by the user (Approve/Reject quick
  replies, header `New promote approval — Dev JC {{1}}`, body vars {{1}}=gate label … {{9}}=amount).

## Components

### Outbound — `whatsapp_service.notify_promote_review(conn, *, dev_jc_id, requestor_uid)`
- Recipients per gate: INV_MGR → all active `inventory_manager` phones; REQUESTOR_BH → the
  `requestor_uid`'s phone (skip if None/no phone).
- Fetch the dev JC (`SELECT * FROM npd_dev_job_cards`) for the body params (number, target FG,
  qty+uom, company, customer, return type, paid, amount).
- Send template `npd_promote_approval` (env `WHATSAPP_TPL_NPD_PROMOTE`, default the same) with
  `header_params=[dev_jc_number]` and `body_params=[gate_label, dev_jc_number, target_fg,
  qty, company, customer, return_type, paid, amount]`. `gate_label` differs per recipient
  ("Inventory manager" / "Requestor (business head)").
- Store one `wa_promote_message(wamid, dev_jc_id, approver_kind, wa_phone)` row per sent
  message (so an inbound tap's `context.id` resolves the gate). Best-effort, never raises.
- Called from `open_promote_request` after commit, parallel to `notify_promote_review_email`.

### Inbound — extend `handle_inbound(conn, from_phone, text, context_id)`
- A tap quotes a wamid (`context_id`). Resolve **promote first**: look the wamid up in
  `wa_promote_message`; if found → promote path; else fall through to the existing
  `wa_review_message` (NPD review) path unchanged.
- Pending-reason reply (free text, no new-action): if the phone has a PROMOTE_REJECT pending
  → treat the text as the reject reason and act; else the existing HOLD-reason path.
- Promote path:
  - Resolve responder phone → `auth_user` (user_id + role_name). No role pre-filter — let
    `act_promote_approval` enforce per-gate authz (inventory_manager ⇒ INV_MGR; bound
    requestor ⇒ REQUESTOR_BH; admin ⇒ either). `approver_kind` comes from the
    `wa_promote_message` row (button tap) or is inferred when typed.
  - **APPROVE** → `act_promote_approval(dev_jc_id, "ACCEPT", user, approver_kind=…)`; reply
    `✓ Approved — your gate is cleared` (or `✓ Promoted` when both clear). On HTTPException
    (already actioned / no pending) → friendly "already actioned" reply (idempotent).
  - **REJECT** → inline reason acts now; else `wa_pending_action` PROMOTE_REJECT armed +
    "reply with the reason"; next reply → `act_promote_approval(REJECT, remarks, approver_kind)`
    (voids the promote). Reply `✓ Rejected`.
  - Typed fallback: `APPROVE <dev-jc#>` / `REJECT <dev-jc#> [reason]` (resolve dev JC by id).
- Button text "Approve"/"Reject" parsed alongside the existing ACCEPT/HOLD verbs.

### DB
- **074_wa_promote_message.sql**: `wa_promote_message(wamid TEXT PK, dev_jc_id BIGINT NOT NULL
  REFERENCES npd_dev_job_cards(id) ON DELETE CASCADE, approver_kind TEXT NOT NULL CHECK IN
  ('INV_MGR','REQUESTOR_BH'), wa_phone TEXT, created_at TIMESTAMPTZ DEFAULT NOW())` + index on
  dev_jc_id. Idempotent; ON CONFLICT(wamid) DO UPDATE on insert (mirrors `wa_review_message`).
- **075_wa_promote_pending.sql** (implemented as a SEPARATE table, not an extension of
  `wa_pending_action`): `wa_promote_pending(wa_phone PK, dev_jc_id BIGINT NOT NULL REFERENCES
  npd_dev_job_cards(id) ON DELETE CASCADE, approver_kind CHECK IN ('INV_MGR','REQUESTOR_BH'),
  created_at)`. **Deviation from the original plan** (which extended `wa_pending_action`): a
  separate table keeps the NPD hold-reason flow 100% untouched (lower regression risk), at the
  cost of a second per-phone pending table. `handle_inbound` checks promote pending (branch b,
  `not context_id`) before falling to the NPD path.

## Error handling
- All outbound is best-effort/config-gated (no WhatsApp config ⇒ log + no-op; never blocks the
  lifecycle). Inbound never raises to the webhook (always 200 so Meta doesn't retry-storm).
- Idempotency via `act_promote_approval` (acts only on PENDING gates) → safe re-taps.

## Testing
- A rollback-verified harness (`scratch/_review_promote_whatsapp.py`, run by a human with
  DATABASE_URL + migrations) monkeypatching `_post`/`_send_template` to capture sends: seed a
  dev JC + pending promote + an inventory_manager/requestor with phones; assert the template +
  params + `wa_promote_message` rows; simulate inbound Approve (gate clears) and Reject-with-reason
  (gate voids); assert idempotent re-tap. Rolls back.
- Adversarial review workflow over the diff (inbound routing, authz, idempotency, param order).

## Out of scope
- Editing/disabling sent buttons (WhatsApp limitation). WhatsApp Flows. Changing the NPD-review flow.
