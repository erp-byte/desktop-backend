# Sample requisition — requestor=Business Head, POC field, full edit parity (design)

Date: 2026-06-16
Scope: **web_replica only** (Next.js). frontend_replica (Electron) intentionally untouched.

## Goal
Three changes to the sample-requisition feature, plus surfacing on the dev job card:
1. The **requestor** dropdown lists **only business heads** (`auth_role.role_name = 'business_head'`), excluding admins.
2. A new **POC** field, auto-filled (read-only) from the **logged-in user's** `full_name` (name only), stored on the requisition and shown on create/edit/view + the dev-JC view.
3. The requisition **edit** form has **full parity** with the create form — every field incl. article/recipe lines (except `sample_type`, which stays locked).

## Decisions (locked with the user)
- Target app: **web_replica only**.
- POC value: **name only** (logged-in user's `full_name`).
- Requestor model: **requestor = the selected business head** (`requestor_user_id`); **POC = the logged-in user** filling the form.
- Edit parity: **everything incl. article lines**; `sample_type` locked.
- JC display: show **POC + Requestor (BH)** on the dev-JC view, from the linked source requisition.
- **Dropdown shows ONLY business heads — no admins** (today the NPD form's filter is `is_admin || role_name==='business_head'`; drop the `is_admin` part, and the new `/business-heads` endpoint returns business heads only).
- **The BH approval is routed to the specific selected requestor BH** (not the whole business_head pool):
  - NPD/TRIAL: the final BH approval is the **promote gate's `REQUESTOR_BH`** — it binds to `requestor_user_id`, so storing the selected BH there routes it to that person automatically.
  - BASIS types: bind `act_bh_approval` + its submit notification to the selected BH (stored as `business_head_user_id`), so only that BH (+ admin) is notified and authorized.

## Database (new migration `app/db/samples/076_requisition_poc.sql`)
```sql
ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS poc_name TEXT;
COMMENT ON COLUMN sample_requisitions.poc_name IS
  'Point of contact: full_name of the logged-in user who created the requisition, snapshotted at creation.';
```
`created_by` already holds the creator's user_id; `poc_name` is the denormalized name snapshot for display. Additive + idempotent.

## Backend (server_replica)
- **New endpoint** `GET /api/v1/sample/business-heads` → `[{user_id, full_name}]`, active `business_head` users **only** (resolved via `auth_user JOIN auth_role WHERE role_name='business_head' AND is_active`; **admins excluded**). Gated on the same permission as creating a requisition so non-admin creators can use it (does not expose the full admin user list).
- **`create_requisition`** (`requisition_service.py`): accept `requestor_user_id` (chosen BH). Set `requestor_user_id` = BH; `business_head_user_id` = BH (so the BH approval binds to them); `requestor_team` = BH `full_name` (display); `poc_name` = creator's `full_name`; `created_by` = creator (unchanged).
- **BH-approval routing to the selected BH:**
  - NPD/TRIAL — no routing change needed: `requestor_user_id = selected BH` already makes the promote gate's `REQUESTOR_BH` (the final BH approval) go to that person.
  - BASIS — `submit_requisition` notifies the specific `business_head_user_id` (in-app + email), and `act_bh_approval` authorizes only that BH (+ admin) instead of the coarse permission alone.
- **`RequisitionUpdate`** (`schemas.py`) + update service: add `requestor_user_id`, `articles`, and remaining create-only fields for full parity. `poc_name` is NOT overwritten on edit. `sample_type` not editable.
- `get_requisition` / list already `SELECT *` → `poc_name` flows automatically. Add a joined `requestor_name` (from `auth_user` on `requestor_user_id`) to the view payload for reliable display.
- **Dev-JC view**: surface `poc_name` + requestor name by joining the source requisition (`source_requisition_id`) in the dev-JC view serializer — no new JC columns.
- Side effect: the promote gate's `REQUESTOR_BH` binds to `requestor_user_id`, now correctly the business head (was the creator).

## Frontend (web_replica)
- `src/lib/sample.ts`: add `poc_name?: string | null` and `requestor_user_id?: number` to `Requisition` / `RequisitionCreate` / update types; add `listBusinessHeads()` API call.
- **Create form** (`sample/new/page.tsx` and the NPD form `_npd-sample-form.tsx`): requestor → `<select>` from `listBusinessHeads()` (business heads only); add read-only POC field showing the logged-in user's name.
- **Edit form** (`sample/[id]/page.tsx` `EditCard`): add all create fields incl. article-line editor (reuse `ArticlePicker`), requestor BH dropdown, read-only POC, billing, customer/dispatch, purpose, pcs/weight/qty.
- **View page** (`sample/[id]`): show POC next to Requestor.
- **Dev-JC view**: show POC + Requestor from the source requisition.
- Mobile-first responsive (grid-cols-1 sm:grid-cols-2; hide-at-breakpoint, not overflow).

## Out of scope
frontend_replica (Electron); changing `sample_type`; the WhatsApp promote inbound fix (already shipped — needs backend restart + a fresh promote).

## Verification
- Backend: rollback-verified scratch harness — create with a BH requestor sets requestor_user_id=BH + poc_name=creator; update accepts articles + all fields; `/business-heads` returns only business heads. tsc --noEmit + eslint on web_replica.
- UAT: create/edit a requisition in the web app; confirm requestor lists only BHs, POC shows my name, edit shows all fields incl. lines, and the dev-JC view shows POC + requestor.
