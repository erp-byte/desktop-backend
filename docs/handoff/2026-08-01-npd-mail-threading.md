# Handoff — NPD mail threading: one trail per transaction

Date: 2026-08-01
Design spec: `server_replica/docs/superpowers/specs/2026-08-01-npd-mail-threading-design.md`

## What this fixes

One NPD transaction used to spray **four separate mail trails** — the requisition notice,
the review mail, the acceptance and the promote approval each rooted their own thread — and
a promote decision (especially one taken over WhatsApp) **mailed nothing at all**.

Now every mail about one transaction lands in a single conversation, a different transaction
gets a different conversation, the promote decision is mailed on every gate, and the trail
closes with the Delivery Challan at dispatch.

The threading approach is a direct port of the Customer Returns pattern
(`customer_returns/services/mail_service.py`) — no new mechanism was invented.

## File manifest

### Backend — `server_replica/`

| File | Lines | Change |
|---|---:|---|
| `app/modules/sample/services/sample_mail_service.py` | 679 | **Rewritten.** Thread key, constant subject, recipient resolution, buttoned/button-less split, 2 new notifiers |
| `app/modules/sample/services/promote_approval_service.py` | 192 | `act_promote_approval` mails the decision post-commit; added `logging` |
| `app/modules/sample/services/npd_dev_service.py` | 1193 | `dispatch_dev_sample` mails the DC link post-commit; added `logging`, captured `dispatch_id` |
| `app/modules/sample/services/approval_service.py` | 243 | Call-site: one outcome mail carrying the reason (was two mails) |
| `app/modules/sample/services/requisition_service.py` | 636 | Call-site: `notify_requisition_event` roots the trail |
| `tests/services/test_npd_mail_threading.py` | 169 | **New.** 5 tests guarding the threading invariants |
| `docs/superpowers/specs/2026-08-01-npd-mail-threading-design.md` | 152 | **New.** Design spec |

### Frontend — `web_replica/`

| File | Lines | Change |
|---|---:|---|
| `src/lib/sample-roles.ts` | 109 | **New cap** `canViewOutpass`; `canOutpass` unchanged |
| `src/app/modules/npd-development/job-cards/[id]/gate-pass/page.tsx` | 366 | Route guard switched to `canViewOutpass` (3 references) |

**No DB migration. No schema change. No new dependency.**

## API surface changes (breaking for callers)

Anything calling these must be updated — all in-repo callers already were:

| Before | After |
|---|---|
| `notify_inventory_informative(conn, req, event=)` | `notify_requisition_event(conn, req, event=, reason=None)` |
| `notify_npd_review_email(conn, req, threaded=True)` | `notify_npd_review_email(conn, req)` — `threaded` removed |
| `notify_requestor_email(...)` | **Removed** — folded into `notify_requisition_event(reason=)` |
| — | **New** `notify_promote_status_email(...)` |
| — | **New** `notify_dev_dispatch_email(...)` |
| `_anchor_msgid(id, email, scope=)` | **Removed** — replaced by `_thread_key` / `_jc_thread_key` |

## How to test

### Backend

```bash
cd server_replica
python -m pytest tests/ -q                                  # expect: 131 passed
python -m pytest tests/services/test_npd_mail_threading.py -v   # the 5 new invariants
```

The new tests use a fake conn + fake SMTP — no DB, no network, no mail sent.

### Frontend

```bash
cd web_replica
npm ci
npx tsc --noEmit          # expect: clean, exit 0
npx eslint src            # expect: 23 pre-existing problems, none in the changed files
```

`npx eslint src` is **not** clean on this repo and was not clean before this change —
14 errors / 9 warnings, nearly all `react-hooks/set-state-in-effect`. One of them is in
`gate-pass/page.tsx` at the query-string effect (line 71), which this change did not touch;
it only moved down 4 lines because a comment was added above it. Compare against `main`
before treating it as new.

### UAT — the thing that actually matters

Raise an NPD requisition and walk it through: **submit → accept → request promote →
approve one gate over WhatsApp → approve the second gate → dispatch**.

Expected: **one Gmail conversation** containing every step, in order. Specifically check
- the WhatsApp gate approval produces a mail (this was the reported bug);
- action buttons appear **only** in mails addressed to a single recipient;
- a second, unrelated requisition opens a **separate** conversation.

## Deploy notes

1. **Backend restart required** — no migration, but the service module changed.
2. **In-flight requisitions break once.** Anything already mid-flow carries the old
   per-scope anchors; its next mail starts the new unified trail. One-time cosmetic split,
   no data fix needed, new requisitions thread from the start.
3. **Mail volume goes up.** One common To/Cc per transaction was the explicit product
   decision: inventory managers now receive the review and promote cards (button-less), and
   npd_team receives the requisition-created notice. Everyone sees the full trail.
4. `WEB_APP_URL` is **not set in `.env`** and falls back to `https://erpcf.in`
   (`app/config.py`). The DC link and the Hold/Reject redirects are built from it — set it
   explicitly if that default is ever wrong for an environment.

## Open items

- **DC is a link, not an attachment.** The dev-JC Delivery Challan is a client-only React
  print page with no server-side generator; attaching a PDF would mean writing one and
  keeping it in sync with the React page. The mail body therefore carries the full dispatch
  summary so it stands alone. Deliberate, agreed with the requester.
- **`canViewOutpass` widens who can open the DC page** to npd_team + business_head +
  inventory_manager + sales (previously npd_team only), because the dispatch mail links
  those people to it. Issuing an outpass is still `canOutpass` (npd_team only), and the
  page has no inventory side effects. Backend already allowed it — `GET
  /npd-dev-job-cards/{id}` needs only `require_permission("sample", action="view")`, which
  `035_sample_roles.sql` grants to BH and inventory_manager.
- **Requestor resolution.** `resolve_recipients` puts `sample_requisitions.requestor_user_id`
  on the To line. Note that `2026-06-16-requisition-poc-requestor-bh-design.md` (requestor =
  selected business head, plus `poc_name` / `business_head_user_id`) was **never
  implemented** — `requestor_user_id` is currently the creating user, and `poc_name` /
  `business_head_user_id` are unused columns. If that spec is implemented later, the To line
  becomes the selected BH automatically with no change here.
