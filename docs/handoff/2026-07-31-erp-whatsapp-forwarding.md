# WhatsApp inbound forwarding — ERP → maintenance ticket backend

**Date:** 2026-07-31 · **Branch:** `feat/whatsapp-maintenance-forward` · **Status:** implemented, not deployed, tests not yet run

The Candor WABA (`phone_number_id 1013172898549548`) is now shared by **three** systems.
Meta permits one webhook URL per app, so every inbound message lands on the ERP webhook,
which fans out to the other two.

```
Meta Cloud API
      │  POST (one subscription, all inbound)
      ▼
ERP  POST /api/v1/sample/whatsapp/webhook          ← the only registered webhook
      ├─ verify X-Hub-Signature-256 → 403 on failure
      ├─ relay raw body ─────────────► maintenance backend   (MAINTENANCE_FORWARD_URL)
      ├─ forward visitor taps ───────► visitor backend       (VISITOR_APPROVAL_FORWARD_URL)
      └─ NPD / promote / reason-capture                       (unchanged)
```

## What gets relayed to maintenance

**Everything except the two flows already living on this webhook.** This is a denylist,
matching maintenance's rule 4 ("anything with `value.messages[]`"). A body is relayed if
**any** message in it is not ERP or visitor traffic.

Excluded — kept by the ERP / visitor system:

| Excluded | Matched on |
|---|---|
| Visitor Management taps | payload `approve_<digits>` / `reject_<digits>` |
| NPD-review + promote-gate taps | payload is the button **text**: `accept` `hold` `approve` `reject` (+ `approved` `rejected` `decline` `declined`), case-insensitive — checked on `interactive.button_reply.id`, `interactive.list_reply.id`, and `button.payload` |
| The typed equivalents | a `text` message whose **first word** is one of those verbs (`ACCEPT 12345678`, `HOLD 12345678 line was down`) |
| Delivery/read receipts | `value.statuses[]` — no `messages[]`, so never matched |

Everything else is relayed: prefixed taps (`mnt:` `tkt:` `qc:` …), unprefixed taps, plain
text, images, audio, video, documents, locations, contacts, stickers, reactions, and any
message type Meta adds later.

Two deliberate calls:

- **Only the first word of a text is checked.** "the compressor was rejected by QC" is a
  fault report, not a promote reject. Matching the verb anywhere would swallow ordinary
  descriptions.
- **Bare `YES` / `OK` / `NO` are relayed**, even though the promote gate accepts them. The
  ERP only *acts* on them when the sender has a pending gate, whereas "yes" answered to a
  maintenance prompt is ordinary traffic — excluding them would break the common case to
  protect the rare one.

An allowlist (`text,image` + prefixes) was implemented first and replaced: the bot's form
answers are free-form, so enumerating shapes just means silently dropping the next one
added. Over-forwarding is cheap — maintenance dedupes on `wamid` and ignores what isn't
theirs — while under-forwarding breaks their conversation with no error anywhere.

## The transport contract

Three properties the maintenance side depends on. Each is pinned by a test.

1. **Byte-identical body.** We POST the *original* request bytes (`httpx content=raw`,
   never `json=`). `metadata`, `contacts`, `entry.id` all intact. Any re-serialisation
   would reorder keys and invalidate the signature below.
2. **`X-Hub-Signature-256` passed through unchanged.** Validates against the bytes we
   send, *if* you hold the same Meta app secret. Maintenance has opted to treat it as
   opaque for v1 — no action needed.
3. **`X-Forward-Secret` on every relayed request.** Shared secret proving the relay came
   from this webhook. Meta's signature can't do that job — it authenticates *Meta*, and
   stays valid on a captured-and-replayed body. Sent only when
   `MAINTENANCE_FORWARD_SECRET` is set; omitted otherwise.

**Decision granularity is per-body, not per-message.** You cannot subset a raw body
without rebuilding it, which breaks (1) and (2). Consequence: if Meta ever batches an ERP
message and a maintenance message into one body, maintenance receives both. Rare;
maintenance ignores what isn't theirs.

**Delivery is fire-and-forget.** The relay runs as a detached task; Meta gets its `200`
immediately. This is deliberate — the target is a free-tier Render instance that
cold-starts 30–60 s, and blocking on that would blow Meta's webhook timeout and trigger
retries, i.e. **duplicate tickets**. Timeout 60 s, one retry on transport error or 5xx
(safe: the receiver dedupes on `wamid`). A 4xx is not retried — a bad secret or wrong
path will not fix itself. After that the relay logs and is dropped; it is not queued.

### Where this actually runs

Meta posts to `https://erpcf.netlify.app/api/v1/sample/whatsapp/webhook`. Netlify does
**not** implement that endpoint — `next.config.ts` rewrites `/api/:path*` to
`http://65.0.86.156/api/:path*`, so the request lands in the FastAPI handler
`app/modules/sample/router.py::whatsapp_webhook_receive`. The relay is implemented there,
in Python. No Netlify function is involved and none is needed.

> **Untested consequence of that hop.** The body reaches FastAPI via
> Meta → Netlify edge → Next.js rewrite → EC2. We forward the bytes *as we received them*,
> which is byte-identical to what Meta sent **only if the proxy chain doesn't touch them**.
> Nothing verifies that today, because our own signature check is disabled. Enabling
> `WHATSAPP_APP_SECRET` proves the whole chain end-to-end: if it validates on our side, the
> bytes survived and the signature we relay is good. Until then, treat guarantee (2) as
> unproven — which is fine, since maintenance is treating the header as opaque for v1.

## Environment variables

Set on the **ERP backend** (FastAPI/uvicorn — see note below). Values are **not** in this
document; they are delivered separately.

| Var | Purpose | Effect if unset |
|---|---|---|
| `MAINTENANCE_FORWARD_URL` | relay target | **entire feature is a no-op** |
| `MAINTENANCE_FORWARD_SECRET` | value of `X-Forward-Secret` | header omitted |
| `WHATSAPP_APP_SECRET` | inbound signature enforcement | **webhook is OPEN** — see below |

> **Three-place rule.** A new env var needs a `Settings` field in `app/config.py`, an entry
> in `app/main.py`'s hydration tuple, **and** a call-time `os.environ` read. pydantic-settings
> loads `.env` into the `Settings` object only, and the lifespan copies an allow-list into
> `os.environ` *after* module import — so a module-level constant freezes to `""` and the
> feature silently no-ops. This exact bug disabled visitor forwarding in prod for weeks.

### `WHATSAPP_APP_SECRET` was never wired up

Found during this work: it was in neither `Settings` nor the hydration tuple, so
`verify_signature()` has been returning `True` for every body. **The webhook is currently
open** — anyone who can reach the public URL can inject inbound and have it relayed to both
tenants. Both legs are now added; the value still has to be set.

Set it **separately from the relay cutover**, and after the relay is confirmed working. A
wrong value 403s *all* inbound — NPD, visitor and maintenance alike — and you will not know
which change broke what if the two land together.

## Changes

| File | Change |
|---|---|
| `app/modules/sample/services/whatsapp_service.py` | `is_maintenance_message`, `has_maintenance_message`, `_post_maintenance`, `forward_maintenance` (+82) |
| `app/modules/sample/router.py` | 8 lines after the JSON parse, before the visitor block |
| `app/config.py` | 4 new `Settings` fields |
| `app/main.py` | hydration tuple + boot log lines |
| `tests/services/test_wa_maintenance_routing.py` | 14 tests (new) |

**No existing line of logic was modified.** NPD, promote and reason-capture see exactly what
they saw before — there is no exclusion step for maintenance traffic, unlike the visitor path.

## Verification

```bash
# pytest is not installed in .venv; the file also runs standalone
.venv/Scripts/python.exe tests/services/test_wa_maintenance_routing.py
.venv/Scripts/python.exe tests/services/test_wa_visitor_routing.py   # regression guard
```

Boot logs to check on every deploy:

```
Maintenance forwarding ENABLED -> https://…/api/whatsapp/webhook [X-Forward-Secret set]
Visitor-approval forwarding ENABLED -> …
WhatsApp inbound signature check ENFORCED          ← currently OPEN
```

Per-relay: `Relayed webhook body to the maintenance backend` on success;
`Maintenance forward rejected: status=…` / `Maintenance forward failed` otherwise.

## Rollout order

The ordering below matters — each step isolates one failure mode.

1. Run the tests. Deploy with `MAINTENANCE_FORWARD_URL` **unset**; confirm the boot log
   says `DISABLED` and NPD / promote / visitor still work. Proves the no-op path.
2. Set `MAINTENANCE_FORWARD_URL` only. Restart. Joint test with the maintenance team: one
   `mnt:` tap and one plain text, both sides watching logs.
3. Set `MAINTENANCE_FORWARD_SECRET` on the ERP side **first**, restart, confirm relays
   still arrive. Only then does maintenance start *enforcing* it. Enforcing before we send
   it black-holes every relay.
4. Set `WHATSAPP_APP_SECRET` last, on its own, and restart. Watch for `ENFORCED`.

**Rollback:** remove `MAINTENANCE_FORWARD_URL` and restart. One line, no code revert.

## Conversation ownership — needs a decision before go-live

Maintenance asked us to pick (a) "maintenance bot owns it, ERP stays silent" or (b) "ERP
owns it and delegates". **Neither is accurate for this number**, because the ERP runs live
NPD and promote-approval conversations on it and those must keep working.

What actually happens today, traced through `handle_inbound`:

| Sender | ERP behaviour | Double reply? |
|---|---|---|
| Not an ERP user | falls through to the visitor forward, **stays silent** | no |
| ERP user, role not `npd_team`/`admin` | `_resolve_reviewer` returns `None` → silent | no |
| `npd_team` / `admin` member, free text | replies `Reply ACCEPT <request#>…` | **yes** |
| Anyone with an armed ERP prompt (`wa_pending_action` / `wa_promote_pending`) typing the reason | consumes it as the hold/reject reason | **yes** |

So we are effectively at (a) for everyone except ERP reviewers and approvers — a small,
known set. Recommended answer to maintenance: **(a) with a carve-out**, ERP stays silent
except on its own NPD/promote conversations.

The two "yes" rows are fixable precisely: skip the relay for a phone that has a row in
`wa_pending_action` or `wa_promote_pending`. One indexed lookup, and it makes
"in the ERP flow" authoritative without exposing a session endpoint. Not implemented —
it needs `forward_maintenance` to become async and take a connection. Say the word.

## Open items

- **Guarantee (2) is unproven** until `WHATSAPP_APP_SECRET` is set — see the proxy-hop note
  above. Do this; it is the only end-to-end check of the byte path.
- Env var naming differs across the two docs (`MAINTENANCE_FORWARD_SECRET` in maintenance's
  code sample vs `WHATSAPP_FORWARD_SECRET` in its footer). Names are local to each side —
  only the **value** and the header name `X-Forward-Secret` have to match. Ours is
  `MAINTENANCE_FORWARD_SECRET`.
- No queue on relay failure. After one retry a dropped relay is gone. Add a queue only if
  Render downtime ever costs a real ticket.
