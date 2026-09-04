# NPD dispatch-date reminders — design

**Date:** 2026-09-04
**Status:** awaiting review
**Scope:** four scheduled notification templates around `sample_requisitions.expected_dispatch_date`, the loop that fires them, and two business-head actions reachable from the mail.

---

## 1. Problem

A sample requisition carries an `expected_dispatch_date` set by BD. Nothing watches it. A request whose date passes just sits open: NPD is not told the deadline is tomorrow, the business head is not told it has slipped, and there is no way to act on a slip except by opening the portal and remembering it exists.

## 2. Goals

- Warn the NPD team, the sales POC and the business head **one day before** a dispatch date.
- Tell NPD and the business head **every day** a dispatch date is past, until it is resolved.
- Let the business head resolve it **from the mail**: cancel the request (with a reason) or move the expected date.

### Non-goals

- No change to how `expected_dispatch_date` is set, or to `confirmed_dispatch_date`.
- No WhatsApp channel (mail only; the WhatsApp gate is a separate mechanism).
- No reminders for anything other than the expected dispatch date.

## 3. What gets chased

One scan, two buckets, both over `sample_requisitions`:

```
deleted_at IS NULL
AND expected_dispatch_date IS NOT NULL
AND status IN ('DRAFT','SUBMITTED','BH_APPROVED','ON_HOLD',
               'IN_PRODUCTION','PACKING','READY_FOR_DISPATCH','PARTIALLY_CONVERTED')
```

| Bucket | Condition |
|---|---|
| `DUE_TOMORROW` | `expected_dispatch_date = <ist_today> + 1` |
| `OVERDUE` | `expected_dispatch_date < <ist_today>` |

The eight statuses are exactly `OPEN_STATUSES` from `web_replica/src/app/modules/sample/dashboard/_build.ts`. A request that reached `INTERNALLY_DISPATCHED`, `GATE_PASS_ISSUED` or `CLOSED` has shipped; `BH_REJECTED` and `CANCELLED` are dead. Neither is chased.

**Dates are IST, not the server's.** Every comparison uses `(NOW() AT TIME ZONE 'Asia/Kolkata')::date`. `CURRENT_DATE` on a UTC host rolls over at 05:30 IST, which would put "tomorrow" on the wrong side of the boundary for half the working day.

## 4. The four mails

All four render through the existing design system in `sample_mail_service` — `_shell`, `_detail_table`, `_kv_rows`, `_key_figures`, `_callout`, `_buttons` — so they cannot drift from the rest of the trail. All free text passes through `_fmt` (HTML-escaped; these mails carry customer names and descriptions).

All four thread into the requisition's existing conversation via `_thread_key(request_id)`, so a reminder lands in the same thread as the request it is about rather than starting a new one.

| # | Builder | Trigger | To | Cc | Buttons |
|---|---|---|---|---|---|
| **T1** | `_due_tomorrow_npd_html` | `DUE_TOMORROW` | `npd_team` | — | none |
| **T2** | `_due_tomorrow_owner_html` | `DUE_TOMORROW` | business head + sales POC | — | none |
| **T3** | `_overdue_npd_html` | `OVERDUE` | `npd_team` | — | none |
| **T4** | `_overdue_owner_html` | `OVERDUE` | business head | sales POC (button-less copy) | **Cancel request**, **Change expected date** |

Recipients come from `resolve_recipients(conn, req)`: `npd` for the team pool, `requestor` for the business head (for an NPD/TRIAL request raised on a BH's behalf, `requestor_user_id` *is* that BH — see `create_requisition`), and `sales_poc` for the POC.

**When a bucket has no recipient**, its mail is skipped and its guard row is *not* claimed, so it retries on the next tick rather than being silently marked sent. Concretely: a requisition whose requestor has no address on file still gets its NPD mail (T1/T3), while T2/T4 are skipped and logged at `warning` naming the requisition — an unaddressable business head is a data problem someone has to fix, not something to swallow. The two halves are independent: the NPD kinds and the owner kinds claim separate guard rows.

### Copy

Header colours follow the existing severity vocabulary: amber for a warning, red for a breach.

**T1 — NPD, due tomorrow.** Eyebrow `DISPATCH DUE TOMORROW`, header `#d97706`.
> This sample request is due for dispatch tomorrow, **&lt;date&gt;**. Sharing it so the trial and its output are ready in time.

Key figures: `Expected dispatch` · `Target article` · `Quantity`.

**T2 — BH + POC, due tomorrow.** Same eyebrow, header `#d97706`.
> The sample request you raised is due for dispatch tomorrow, **&lt;date&gt;**. The NPD team has been notified. If the date needs to move, change it on the portal before it slips.

**T3 — NPD, overdue.** Eyebrow `DISPATCH DATE PASSED`, header `#dc2626`. Informatory, no buttons.
> This sample request has passed its expected dispatch date of **&lt;date&gt;** — **&lt;n&gt; days overdue**. Its business head has been asked to cancel it or set a new date.

**T4 — BH, overdue.** Same eyebrow and header, with actions.
> The sample request you raised has passed its expected dispatch date of **&lt;date&gt;** — **&lt;n&gt; days overdue**. Tap **Change expected date** to set a new one, or **Cancel request** to close it with a reason. It will keep reminding you daily until one of those happens.

Footer `_ACTION_FOOTER` on the BH copy, `_TRAIL_FOOTER` on the button-less one. The overdue day count is `ist_today − expected_dispatch_date`.

## 5. The two actions

Both follow `_bh_signoff_reject_url`: the mail links into the **web app**, which opens a dialog and submits to an email-authenticated endpoint. That is what makes a real date picker and a real reason box possible — the recipient never types a wire format.

```
Cancel        {WEB}/modules/sample/{id}?req_cancel={request_id}&email={bh}&t={token}
Change date   {WEB}/modules/sample/{id}?req_redate={request_id}&email={bh}&t={token}
```

**Both links are HMAC-signed** through the existing `email_link_token.sign`, with new bindings:

```
sign("req_cancel", request_id, email)
sign("req_redate", request_id, email)
```

The existing BH *reject* link is deliberately unsigned, on the stated grounds that rejecting is non-escalating. That reasoning does not carry over: **cancel is destructive and irreversible** (`CANCELLED` is terminal), so an unsigned link would let anyone who guesses an 8-digit `request_id` and a BH's address kill a live request. Both new links are signed.

### Endpoints

Two new public endpoints on the sample router, mirroring `POST /email/bh-signoff-reject`:

| Endpoint | Body | Behaviour |
|---|---|---|
| `POST /email/requisition-cancel` | `request_id`, `email`, `t`, `reason` | verify token → resolve requisition → assert `email` is the bound BH → `cancel_requisition(reason=...)` |
| `POST /email/requisition-redate` | `request_id`, `email`, `t`, `expected_dispatch_date` | verify token → same auth → `update_requisition(expected_dispatch_date=...)` |

Both reuse the existing services, so the status guard (`_assert_transition`), the mandatory-reason check and the audit write all come for free. `cancel_requisition` already rejects a blank reason with 422; the dialog disables submit on empty input so that is a backstop, not the UX.

A logged-in BH clicking from mail on a machine with a session should use the ordinary session endpoints. The portal picks between the two exactly as `submitReject` already does on the job-card page.

### Portal dialogs

On `modules/sample/[id]`, mirroring the existing `?bh_reject=` handling:

- `?req_cancel=` → the page's existing cancel modal, pre-bound to the email flow. Reason is required.
- `?req_redate=` → a new dialog with `<input type="date">` — a **native date picker**, so there is no format for the BH to get wrong. On the wire it emits `YYYY-MM-DD`, which is what the backend's `Optional[date]` expects, and it is the same control `DispatchPlanCard` already uses for this very field.

Both strip their query params via `history.replaceState` after opening, so a refresh does not reopen them.

**Re-arming.** A successful redate deletes that requisition's `OVERDUE_*` guard rows. The new date then earns a fresh `DUE_TOMORROW` mail, and if the new date is also missed, a fresh overdue chase. Without this the daily chase would go quiet against the *new* date because the old rows still say "already sent".

## 6. The send-once guard

New table, migration `087_sample_dispatch_reminder_log.sql`:

```sql
CREATE TABLE IF NOT EXISTS sample_dispatch_reminder_log (
    id              BIGINT PRIMARY KEY,   -- app-supplied new_short_time_id, the house
                                          -- pattern (see 078); NOT a SERIAL
    requisition_id  BIGINT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    kind            TEXT   NOT NULL,   -- DUE_TOMORROW_NPD | DUE_TOMORROW_OWNER
                                       -- OVERDUE_NPD      | OVERDUE_OWNER
    sent_on         DATE   NOT NULL,   -- the IST day it was sent
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (requisition_id, kind, sent_on)
);
```

The row is inserted `ON CONFLICT DO NOTHING` **before** the mail is sent, and the mail goes only if the insert took. Three consequences:

1. **Idempotent.** A retried tick, a restart, or an hourly tick that runs 14 times a day still sends once.
2. **Safe under multiple replicas.** If several instances tick together, the unique index decides which one sends. This is stronger than `promote_reminder_loop`, whose docstring notes it *"assumes a SINGLE persistent instance … running several would double-send, since the due-gate SELECT takes no row lock."*
3. **Daily chase falls out.** `sent_on` is part of the key, so tomorrow is a new row and the overdue chase repeats — without a separate counter.

It also leaves an audit trail of what was chased and when.

Hand-applied like every `samples/` migration (see the header of `072`), so the service probes `information_schema` for the table and no-ops when it is absent — an unmigrated environment sends nothing rather than 500-ing the loop every tick.

## 7. The loop

New `app/modules/sample/services/dispatch_reminder_service.py`, exposing `dispatch_reminder_loop(pool)`, started in `app/main.py`'s lifespan beside the three loops already there:

```python
bg_tasks.append(asyncio.create_task(dispatch_reminder_loop(pool)))
```

It follows the same pattern as `promote_reminder_loop`: env-tunable tick, `try/except` per tick so a bad tick cannot kill the loop, `CancelledError` handled for clean shutdown, and a startup log line naming the resolved config. One deliberate divergence: it ticks *before* its first sleep, where `promote_reminder_loop` sleeps first. There, sleeping first only delays a resend if the process recycles early; here it would silently disable the whole feature, since a process that recycles faster than the tick (deploy loops, health-check flapping, `uvicorn --reload`) would never reach a single scan. The send-once guard makes an immediate first tick idempotent, and the hour gate still stops a 02:00 restart from mailing anyone.

**No new dependency.** APScheduler was considered and is not needed — the codebase already runs periodic in-process work this way, and adding a scheduler library alongside three hand-rolled loops would add a second idiom for no gain.

Each tick:

1. If `hour(IST) < SAMPLE_REMINDER_HOUR`, skip. Stops a 02:00 restart mailing people at 2am.
2. Scan both buckets.
3. Per requisition per kind: claim the guard row; if claimed, render and send.

Ticking hourly rather than once a day is deliberate. The loop lives and dies with the web process, so a fixed daily alarm would be missed entirely by a restart or a spin-down at the wrong minute. Hourly + the day-guard turns that into *at worst a delay*: the first tick after the app is up on a given day sends, the rest no-op.

### Configuration

| Env | Default | Meaning |
|---|---|---|
| `SAMPLE_REMINDER_ENABLED` | `1` | master off-switch |
| `SAMPLE_REMINDER_TICK_MIN` | `60` | minutes between ticks (floored at 15) |
| `SAMPLE_REMINDER_HOUR` | `7` | earliest IST hour to send |

`scan_and_send(conn, *, today, dry_run=False)` returns per-kind counts. With `dry_run=True` it claims nothing and sends nothing, and reports the bucket size rather than a count of addressable mails — it does not resolve recipients, so the number can include a requisition already sent today by an earlier tick, or one whose business head has no address on file. That over-counts in the safe direction: enough to size the first batch on real data before the switch is turned on, without needing to be exact.

### Known limitation

Like `dispatcher_loop`, `broadcaster_loop` and `promote_reminder_loop`, this **only ticks under a persistent server** (uvicorn / ECS / the `render.yaml` web service). On the Lambda/Mangum path (`handler = Mangum(app, lifespan="on")`, `Dockerfile.lambda`) it does not run. This is inherited from the chosen approach, not introduced by it, and is called out here so it is not discovered in production. If the deployment moves to Lambda, the scan needs an external trigger — the service is deliberately split so `scan_and_send(conn, *, today)` can be called from an endpoint or a `scripts/` entrypoint without the loop.

## 8. Testing

Unit tests against a fake connection, in the house style of `tests/services/test_req_npd_dispatch_parts.py` (no DB):

- **Bucketing** — a request due tomorrow is `DUE_TOMORROW`; due today is neither; yesterday is `OVERDUE`; each of the five non-open statuses is excluded; a null date is excluded.
- **IST boundary** — the scan compares against the IST date, verified at a UTC instant that falls on a different IST day.
- **Guard** — two consecutive `scan_and_send` runs on the same day send once; a second day sends again; a claimed row for one kind does not suppress the other three.
- **Unmigrated** — with no `sample_dispatch_reminder_log` table the tick no-ops and sends nothing.
- **Templates** — each of the four renders without raising; T4's BH copy contains both action URLs and the trail copy contains neither; a requisition whose customer name contains `<script>` is escaped in the output.
- **Tokens** — `req_cancel` / `req_redate` round-trip, and a token for one action does not verify for the other or for a different `request_id`.
- **Endpoints** — cancel with a blank reason is 422; a bad/absent token is rejected; an email that is not the bound BH is rejected.

Frontend: `buildDispatchHistory`-style pure coverage is not applicable here; the two dialogs are wired the same way as the existing `bh_reject` dialog and verified by typecheck, lint and build.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Mail volume on a backlog of stale requests | Chases only open requests with a date set; the BH has two one-tap ways to stop it. `SAMPLE_REMINDER_ENABLED=0` kills it outright. |
| Loop does not run on Lambda | Stated above; `scan_and_send` is callable without the loop. |
| Multi-replica double-send | The unique guard decides the winner. |
| A forged cancel link destroys a live request | Both links HMAC-signed and bound to `(action, request_id, email)`; the endpoint re-checks the email is the bound BH. |
| Restart at the wrong minute misses the day | Hourly tick + day-guard. |
| First run mails every overdue request at once | Accepted and intended — that backlog is the problem being solved. Deploy with `SAMPLE_REMINDER_ENABLED=0`, check the scan's dry-run count, then switch it on in a chosen window so the first batch is expected rather than a surprise. |
| An unaddressable business head silently stops the chase | The owner mail is skipped without claiming its guard row and logged at `warning`, so it retries and stays visible instead of being marked sent. |

## 10. Files

**New**
- `app/db/samples/087_sample_dispatch_reminder_log.sql`
- `app/modules/sample/services/dispatch_reminder_service.py`
- `tests/services/test_dispatch_reminders.py`
- `web_replica/src/app/modules/sample/[id]/_RedateDialog.tsx`

**Changed**
- `app/modules/sample/services/sample_mail_service.py` — four builders, four senders, two signed URL helpers
- `app/modules/sample/router.py` — two email endpoints
- `app/modules/sample/schemas.py` — two request bodies
- `app/modules/sample/services/email_link_token.py` — document the two new bindings
- `app/main.py` — start the loop
- `web_replica/src/app/modules/sample/[id]/page.tsx` — `?req_cancel` / `?req_redate` handling
- `web_replica/src/lib/sample.ts` — two client calls

## 11. Open questions

None blocking. Two defaults chosen without asking, easily changed:

- Reminders run at **07:00 IST**.
- T2 addresses the business head and the sales POC together in `To`, rather than the BH in `To` with the POC in `Cc`.
