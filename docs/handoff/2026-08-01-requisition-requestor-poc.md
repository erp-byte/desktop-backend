# Handoff — requisition requestor (business head) + sales POC

Date: 2026-08-01
Spec: `docs/superpowers/specs/2026-06-16-requisition-poc-requestor-bh-design.md` (goals 1–2,
with the POC field superseded by an editable **sales POC**)

## What this delivers

A requisition now records **three** distinct people instead of conflating them:

| Field | Meaning |
|---|---|
| `requestor_user_id` | the **business head the request is raised FOR** (was: whoever created it) |
| `business_head_user_id` | the same BH — binds the BASIS approval gate to that one person |
| `requestor_team` | display mirror of that BH's name |
| `sales_poc_user_id` / `_name` / `_email` | **NEW** — the sales point of contact. Defaults to whoever raised the request, **editable**, Cc'd on the mail trail |
| `created_by` | unchanged — the creator's user_id |

It matters beyond display: the NPD mail trail puts `requestor_user_id` on the To line and
the sales POC on the Cc, and the promote gate's `REQUESTOR_BH` binds to
`requestor_user_id` — so all of it routes to real, chosen people.

**Goal 3 of the spec (edit-form full parity incl. article-line editing) is NOT in scope
and remains unimplemented.**

## File manifest

### Backend — `server_replica/`

| File | Change |
|---|---|
| `app/db/samples/085_requisition_sales_poc.sql` | **New.** Adds the `sales_poc_*` trio; documents the new meaning of the two existing id columns |
| `app/modules/sample/services/requisition_service.py` | `list_business_heads` returns `{user_id, full_name}`; new `list_sales_pocs`, `_assert_business_head`, `_resolve_sales_poc`, `has_sales_poc_columns`; create + update wire requestor/BH/sales-POC |
| `app/modules/sample/services/approval_service.py` | `act_bh_approval` — only the bound BH (or an admin) may clear the gate |
| `app/modules/sample/services/npd_dev_service.py` | `get_dev_job_card` returns `source_requestor_name`, `source_sales_poc_name/_email` |
| `app/modules/sample/services/sample_mail_service.py` | Sales POC on the Cc of every trail mail + a "Sales POC" row on the requisition, promote and dispatch cards |
| `app/modules/sample/schemas.py` | `requestor_user_id` + `sales_poc_*` on the three requisition models |
| `app/modules/sample/router.py` | **New** `GET /api/v1/sample/sales-pocs` |
| `tests/services/test_npd_mail_threading.py` | +2 tests covering the sales POC Cc and card rendering |

### Frontend — `web_replica/`

| File | Change |
|---|---|
| `src/lib/sample.ts` | `BusinessHead` + `SalesPoc` types, `listSalesPocs()`, `sales_poc_*` on the requisition types |
| `src/lib/npd-dev.ts` | `source_requestor_name`, `source_sales_poc_name/_email`, `source_requisition_id` on `DevJobCard` |
| `src/app/modules/npd-development/_npd-sample-form.tsx` | BH dropdown carries `user_id`; **editable Sales POC picker** defaulting to the signed-in user |
| `src/app/modules/npd-development/page.tsx` | Queue edit form's BH dropdown carries `user_id` |
| `src/app/modules/sample/[id]/page.tsx` | Edit form carries `user_id`; Sales POC on the view |
| `src/app/modules/npd-development/job-cards/[id]/page.tsx` | Requestor + Sales POC on the dev-JC header (read-only) |

**No new dependency.** One hand-applied migration.

## BREAKING: `GET /api/v1/sample/business-heads`

Response changed from `["Name", …]` to `[{user_id, full_name}, …]`. The frontend client
normalizes **both** shapes, so a frontend deployed ahead of the backend still renders
names (it just can't send `requestor_user_id` until the backend follows). Any other
consumer must be updated.

## Deploy order

1. **Apply the migration by hand** — `samples/` migrations are not enumerated by
   `scripts/migrate.py` (see the header of `072_requisition_billing_fields.sql`):
   ```bash
   psql "$DATABASE_URL" -f app/db/samples/085_requisition_sales_poc.sql
   ```
   Additive + idempotent; safe to re-run.
2. Deploy the backend (restart required).
3. Deploy the frontend.

The code tolerates step 1 being skipped — every `sales_poc_*` read and write sits behind
an `information_schema` guard, so an unmigrated environment keeps working with the POC
blank rather than 500ing. **That guard is a safety net, not a substitute for running it.**

> **Note on the migration number.** An earlier revision of `085` added a read-only
> `poc_name` column. It was superseded by the editable `sales_poc_*` trio before release
> and never carried data, so `085_requisition_sales_poc.sql` drops it guarded
> (`DROP COLUMN IF EXISTS`) — a no-op on any database that never saw that revision.
> The interim file `085_requisition_poc.sql` has been deleted; if you pulled it earlier,
> make sure it is gone so it can't be applied.

> ⚠️ Separately: **17 sample migrations (068–084) are on disk but never registered in
> `scripts/migrate.py`.** They are applied by hand, which is why this codebase defends
> itself with `information_schema` checks. Worth a cleanup — nothing here depends on it.

## Behaviour notes

- **Backward compatible.** Omitting `requestor_user_id` keeps the pre-085 behaviour: the
  creator stays the requestor and `business_head_user_id` is left **NULL** rather than
  guessed. Pre-085 requisitions carry no binding, so the BH gate keeps its old pool
  behaviour for them — no backfill needed.
- **The sales POC only moves when a patch names one.** Defaulting to the signed-in user on
  every edit would silently steal the POC from whoever was set at creation each time an
  unrelated field was touched by someone else.
- **A named sales POC has name + email re-read from `auth_user`**, so the stored snapshot
  can never disagree with the account. Free-text name/email is still accepted for a POC
  without a login — the same escape hatch Customer Returns has.
- **Any user may be the sales POC**, not just `sales` role holders. Unlike the requestor
  BH it drives no approval gate — only display and a Cc — so restricting it would add
  friction for no safety gain. The picker lists `sales` users plus "me".
- **Non-BH requestors are rejected** with `invalid_requestor` (422). The dropdown offers
  BHs only, so anything else is a hand-crafted payload — and it would bind the approval
  gate to someone who could never clear it.

## Known gap — deliberately not done

The spec also asks that submitting a **BASIS** requisition notify the specific BH in-app.
Not implemented: `store_alert` is **team-scoped** (`target_team`) with no per-user target,
so it needs new plumbing rather than a one-line change. Today a BASIS submit emits no BH
alert at all, so nothing regressed — the gap is pre-existing. The *authorization* half
(only the bound BH may approve) **is** implemented, which is the part that enforces
routing.

## Verification performed

| Check | Result |
|---|---|
| `python -m pytest tests/ -q` | **133 passed** |
| `npx tsc --noEmit` | clean, exit 0 |
| `npx eslint src` | 23 problems — **identical to the pre-change baseline**, none in changed files |
| Rollback-verified DB harness | create / update / POC re-point / reject / backward-compat all pass |
| Dev-JC serializer, both migration states | pre-085 and post-085 branches both execute |
| Mutation test | removing the dev-JC POC propagation makes the mail test fail, as it should |

The DB harness runs inside an always-rolled-back transaction and creates temp users so the
creator and the BH have **distinct** ids — without that, `created_by == creator` passes
trivially and proves nothing.

## UAT

1. As **sales or admin**, raise an NPD request — Requestor lists business heads only;
   Sales POC defaults to you and can be changed.
2. Open the request — Requestor is the chosen BH, Sales POC is shown.
3. Check the mail: every mail in the trail Ccs the sales POC and shows a **Sales POC** row.
4. Open the dev job card raised from it — Requestor + Sales POC in the header.
5. As a **different** BH, try to approve a BASIS requisition bound to someone else →
   expect `403 not_the_approver`. As the bound BH (or admin) → succeeds.
