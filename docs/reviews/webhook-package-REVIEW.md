---
reviewed: 2026-04-18
depth: standard
files_reviewed: 10
files_reviewed_list:
  - app/webhooks/__init__.py
  - app/webhooks/event_bus.py
  - app/webhooks/signer.py
  - app/webhooks/dispatcher.py
  - app/webhooks/broadcaster.py
  - app/webhooks/router.py
  - app/webhooks/ws_router.py
  - app/webhooks/events.py
  - app/db/002_webhooks.sql
  - app/config.py
findings:
  critical: 2
  high: 6
  medium: 8
  low: 6
  total: 22
status: issues_found
verdict: needs-fixes
---

# Webhook & WebSocket Event System — Code Review

**Reviewed:** 2026-04-18
**Depth:** standard
**Files Reviewed:** 10
**Status:** issues_found
**Verdict:** **NEEDS FIXES** — two Critical (SSRF, unauthenticated WS token endpoint posture) and six High findings should be resolved before this package handles production traffic. The architecture is clean and the previously-reported bug fixes (JWT `sub` as string, admin `"*"` bypass) are correctly applied. The remaining issues concentrate on SSRF, retry correctness, connection-pool starvation, and `asyncio.create_task` fire-and-forget patterns.

---

## Executive Summary

The webhook package follows a clean pub/sub design: a single in-process `asyncio.Queue`-backed `EventBus` fans out to a webhook dispatcher (HTTP-out) and a WebSocket broadcaster (push-to-app). Named event constructors in `events.py` keep services decoupled from the bus. The `deferred_events()` context manager thoughtfully ties publish to transaction commit.

Two issues warrant blocking attention:

1. **SSRF on user-provided webhook URLs** (`router.py` create/update/test). `httpx.AsyncClient` with no URL validation will happily POST to `http://169.254.169.254/` (AWS metadata), `http://localhost:…/admin/*`, or private RFC1918 ranges, with a valid HMAC. On Lambda this leaks the IAM role token.
2. **`_empty `asyncpg pool, `max=3`** combined with the dispatcher's "one pool connection per delivery update" pattern and `MAX_CONCURRENT_DELIVERIES=20` will deadlock the request path whenever more than 2 deliveries are in-flight against an unresponsive webhook (each `_deliver` call holds a pool connection during DB writes while retrying a 10s HTTP request).

Other high-severity items: unbounded `asyncio.create_task` in `_dispatch_event`, f-string SQL construction in `list_deliveries` (safe today but fragile), retry endpoint doesn't wait for prior 'pending' flip to commit before dispatching, and silent event loss on subscriber queue overflow.

The two correctness fixes called out in the prompt are verified below:

- **JWT `sub` round-trip:** `ws_router.py:35` encodes `str(user.user_id)`, and `ws_router.py:71` decodes via `int(payload["sub"])`. PyJWT 2.12+ strict-sub compliance is satisfied. (No regression found.)
- **Admin entity bypass:** `broadcaster.py:81-82` correctly checks `"*" in role_prefixes` before enforcing `info["entity"] != event.entity`, so admin sees events across entities and with `entity=None`. (No regression found.)

---

## Critical Issues

### CR-01: SSRF — webhook URLs are user-provided but never validated

**File:** `app/webhooks/router.py:42-57, 77-98, 225-253` (and `dispatcher.py:114`)
**Severity:** Critical
**Category:** security

**Issue:** `create_endpoint`, `update_endpoint`, and `test_webhook` accept an arbitrary `url` string and hand it to `httpx.AsyncClient.post()` with a valid HMAC and the endpoint's secret. There is no:

- scheme allow-list (`https` only)
- host/IP deny-list (`127.0.0.0/8`, `169.254.169.254`, `::1`, `fc00::/7`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `metadata.google.internal`, `metadata.azure.com`)
- DNS rebinding protection (resolve once, pin to public IPs)
- request size / redirect cap (httpx default follows up to 20 redirects by default? actually 0 — but the test endpoint accepts any URL with no validation either)

On Lambda this is particularly acute: any authenticated user with `webhooks:create` permission can force the Lambda to POST `http://169.254.169.254/latest/api/token` (IMDSv2) or to arbitrary internal services reachable from the Lambda's VPC ENI — plus the HMAC header is attacker-controlled per-endpoint.

**Fix:** Add a `_validate_webhook_url()` helper and call it on create/update/test and at dispatch time (defense in depth — secrets may be leaked otherwise):

```python
# app/webhooks/url_guard.py
import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"https"}  # plus "http" in dev only, via settings flag

def validate_webhook_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Malformed URL")
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Only https:// URLs are accepted")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")

    # Resolve and block private / link-local / loopback / multicast.
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise ValueError("Hostname did not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError("URL resolves to a non-public IP")

    # Block AWS/GCP/Azure metadata hostnames explicitly
    if parsed.hostname.lower() in {"metadata.google.internal",
                                   "metadata.azure.com",
                                   "metadata"}:
        raise ValueError("Blocked metadata hostname")
```

Then in `router.py`:

```python
from .url_guard import validate_webhook_url

async def create_endpoint(body: EndpointCreate, request: Request, user=...):
    try:
        validate_webhook_url(body.url)
    except ValueError as e:
        raise HTTPException(400, f"Invalid webhook URL: {e}")
    ...
```

And in `dispatcher._deliver`, guard again (endpoints may have been created before validation was added, or an attacker may have edited directly via SQL):

```python
try:
    validate_webhook_url(sub["url"])
except ValueError:
    logger.warning("Skipping delivery to invalid/blocked URL: %s", sub["url"])
    # Mark delivery as failed with reason
    return
```

Also consider pinning the resolved IP (resolve once, pass `transport=httpx.HTTPTransport(...)` with `local_address`/DNS caching) to defeat DNS rebinding between validation and connect.

---

### CR-02: `asyncpg` pool starvation → request-path deadlock under webhook retry storm

**File:** `app/webhooks/dispatcher.py:93-137`, `app/config.py` (`max=3` implicit), compound with `router.py`
**Severity:** Critical
**Category:** bug

**Issue:** The shared pool is `min=1, max=3` (per prompt). `_deliver` holds a connection during each `INSERT webhook_delivery` (attempt 0) and again on every `UPDATE webhook_delivery` per attempt. `MAX_CONCURRENT_DELIVERIES = 20` gates the HTTP post but *not* the DB handle usage — however the actual pool-exhaustion trigger is subtler:

`_dispatch_event` starts one delivery task per matching subscription via `asyncio.create_task(_throttled_deliver(...))`. Each task eventually runs `_deliver`, which:

1. acquires pool → `INSERT` → releases
2. `await client.post(...)` — 10 s timeout
3. acquires pool → `UPDATE` → releases
4. `await asyncio.sleep(BACKOFF_SECONDS[...])` — up to 90 s
5. loop back to step 2

In each iteration the task briefly re-acquires the pool. If an inbound HTTP request also wants a connection during that brief window, it may still get one. But the real issue is: **`asyncio.create_task` inside `_dispatch_event` is unbounded**. If 100 events fire in a second with 5 matching subscribers each, 500 tasks are created. Each task holds the semaphore; the 481st task sits in `_delivery_semaphore`. Meanwhile 20 concurrent `INSERT`s against a pool of size 3 serialize waiters, and because tasks are "sticky" (same task tries to reacquire across retries), the request path will see transient pool timeouts.

Worse, **if the whole Lambda/process is cancelled during `asyncio.sleep(90)`, delivery tasks vanish silently** — there is no persistent "claim" on the delivery row and no worker startup recovery that picks up `status='pending'` rows left by a crash (see HI-04).

**Fix:**

1. Bound `_dispatch_event`'s outgoing task creation: either use a persistent `asyncio.Queue` with a fixed pool of worker coroutines (recommended), or `asyncio.create_task` guarded by a second semaphore sized ≤ pool_max−1.
2. Bump `asyncpg` pool `max` to at least 10 in the settings used for the web tier — 3 is too low once you have a dispatcher, broadcaster, request path, and retries all competing.
3. Move delivery retries out of the request process: workers should pick up `status='pending'` rows from the delivery table (see HI-04) rather than holding state in RAM across a 90 s sleep.

Interim mitigation (before the queue refactor):

```python
# event_bus.py or dispatcher.py
_dispatch_concurrency = asyncio.Semaphore(8)

async def _dispatch_event(client, pool, event):
    async with _dispatch_concurrency:
        ...  # existing fetch + per-sub tasks
```

---

## High Issues

### HI-01: Unbounded `asyncio.create_task` fire-and-forget in dispatcher

**File:** `app/webhooks/dispatcher.py:57-60`
**Severity:** High
**Category:** bug

**Issue:** `_dispatch_event` creates one task per matching subscription without retaining a reference. Python's `asyncio` only holds weak refs to tasks created by `create_task`; they can be **garbage-collected mid-flight** if the event loop has no other strong refs. The canonical workaround is to stash task references in a set.

Additionally, exceptions inside `_throttled_deliver` surface only via `logger.exception` inside `_deliver`, but *construction* exceptions (e.g., `json.dumps` fails on a payload) propagate into the task, which then dies silently with an "exception was never retrieved" warning (or not, depending on Python version).

**Fix:**

```python
_inflight_tasks: set[asyncio.Task] = set()

def _spawn(coro):
    t = asyncio.create_task(coro)
    _inflight_tasks.add(t)
    t.add_done_callback(_inflight_tasks.discard)
    return t

async def _dispatch_event(client, pool, event):
    ...
    for sub in subs:
        _spawn(_throttled_deliver(client, pool, sub, event))
```

Same pattern needed in `router.retry_delivery:221` for `retry_single_delivery`.

---

### HI-02: Retry endpoint fire-and-forget before response = lost dispatch on crash

**File:** `app/webhooks/router.py:189-222`
**Severity:** High
**Category:** bug

**Issue:** The endpoint flips the row to `status='pending'`, then spawns `retry_single_delivery` via `asyncio.create_task` and returns 200 immediately. If the process dies within the next ~100 seconds (retry backoff + HTTP), the DB reflects `attempts=0 pending` but no worker will ever pick it up — there is no startup scan for orphaned `pending` deliveries. The user sees `{"retried": true}` but the retry never happened.

Also: `pool.acquire()` is called twice sequentially (lines 194-203 then 205-209) instead of reusing the single connection; needless round-trip.

**Fix:**

1. Short-term: scan `webhook_delivery WHERE status='pending' AND last_attempt_at < now()-interval '5 minutes'` at startup and re-dispatch.
2. Long-term: make the dispatcher the sole actor on delivery rows. Retry endpoint just flips `status='pending'` and returns; a delivery-reaper worker polls for pending rows.

```python
# Combine the two acquires:
async with pool.acquire() as conn:
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM webhook_delivery WHERE id=$1 AND status IN ('failed','exhausted') FOR UPDATE",
            delivery_id,
        )
        if not row:
            raise HTTPException(404, "...")
        ep = await conn.fetchrow(
            "SELECT id AS endpoint_id, url, secret, entity FROM webhook_endpoint WHERE id=$1",
            row["endpoint_id"],
        )
        if not ep:
            raise HTTPException(404, "Associated endpoint no longer exists")
        await conn.execute(
            "UPDATE webhook_delivery SET status='pending', attempts=0 WHERE id=$1",
            delivery_id,
        )
```

---

### HI-03: Event loss when `_fan_out` target queue is full

**File:** `app/webhooks/event_bus.py:50-58`
**Severity:** High
**Category:** bug

**Issue:** When a subscriber's queue is full (`maxsize=1000`), `put_nowait` raises `QueueFull` and the event is silently dropped with only a warning log. For dispatcher (webhook-out, durable expected) and broadcaster (UI, best-effort acceptable) this is very different behavior. A brief stall in webhook delivery (webhook receiver is slow) causes the dispatcher queue to fill → further events never hit the DB as `pending`, so there is no retry path either — they vanish.

**Fix:** Persist-first for dispatcher. Either:

- move the `INSERT webhook_delivery` out of `_deliver` into `_dispatch_event`, so matching subscriptions get a DB row *before* the task goes into the in-memory semaphore queue; OR
- give the dispatcher a higher-priority path: when its queue is near-full, `await q.put(event)` with a bounded timeout instead of `put_nowait`, so the publisher applies backpressure.

```python
# event_bus.py — per-subscriber backpressure
async def _fan_out(self, event):
    async with self._lock:
        subs = list(self._subscribers)
    for q in subs:
        try:
            await asyncio.wait_for(q.put(event), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.QueueFull):
            logger.error("Dropping event %s (subscriber backlogged)", event.event_id)
```

For durable delivery, record the event into `webhook_delivery` inside `events.py` constructors (or a new `event_outbox` table) before publishing — transactional outbox pattern.

---

### HI-04: No recovery on dispatcher/broadcaster crash — pending deliveries orphaned

**File:** `app/webhooks/dispatcher.py:22-38`, `app/main.py:51-52`
**Severity:** High
**Category:** bug

**Issue:** The dispatcher's state (subscription → retry loop) lives entirely in-memory. If the process dies mid-retry (OOM, redeploy, Lambda cold-start boundary), rows with `status='pending'` or `status='failed'` with `attempts<MAX_ATTEMPTS` become zombies. There is no startup scan.

Additionally, because the bus is process-local, **multi-worker or multi-Lambda-container deployment will fan out the event once per worker** (if all subscribe to the bus) or not at all (if only one worker sees the publish). The docstring in `events.py` does not warn about this.

**Fix:**

1. Add a startup coroutine that scans `webhook_delivery WHERE status IN ('pending','failed') AND attempts < 3` and re-enqueues them.
2. Document the "single-process" constraint prominently in `event_bus.py` docstring and `FRONTEND_WEBHOOK_INTEGRATION.md`.
3. For any multi-worker plan, move to an outbox table + database `LISTEN/NOTIFY` or a proper queue.

```python
# dispatcher.py
async def _recover_pending(pool, client):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT d.id AS delivery_id, d.event_type, d.event_id, d.payload,
                   e.id AS endpoint_id, e.url, e.secret, e.entity
            FROM webhook_delivery d
            JOIN webhook_endpoint e ON e.id = d.endpoint_id
            WHERE d.status IN ('pending','failed') AND d.attempts < $1
            ORDER BY d.created_at
            LIMIT 500
        """, MAX_ATTEMPTS)
    for r in rows:
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        event = Event(
            event_type=r["event_type"], entity=r["entity"],
            payload=payload.get("data", payload),
            event_id=r["event_id"],
            actor=payload.get("actor", "system"),
        )
        _spawn(_throttled_deliver(client, pool, r, event))

async def dispatcher_loop(pool):
    sub = await event_bus.subscribe()
    async with httpx.AsyncClient(timeout=10) as client:
        await _recover_pending(pool, client)
        try:
            while True:
                ...
```

---

### HI-05: SQL built via f-string in `list_deliveries` — safe today, brittle

**File:** `app/webhooks/router.py:165-186`
**Severity:** High (defense-in-depth)
**Category:** security

**Issue:** The `WHERE` clause and `LIMIT/OFFSET` placeholders are composed via f-string concatenation. The current code is not injectable because `conditions` strings contain only hardcoded column names, and parameters are passed separately. But: any future edit that appends a query-param-derived filter name into `conditions` would introduce SQL injection. Stylistically and for review scaffolding, f-string SQL is a red flag.

Also, `where = " AND ".join(conditions) if conditions else "TRUE"` — fine, but the `$1` index in `f"... LIMIT ${idx} OFFSET ${idx+1}"` depends on correct manual counter bookkeeping that is easy to break.

**Fix:** Either move to a small builder helper, or use `asyncpg.utils.quote_ident` / a tiny library. At minimum, assert `conditions` entries are drawn from a whitelist:

```python
_ALLOWED = {"endpoint_id", "event_type", "status"}
filters = {k: v for k, v in [("endpoint_id", endpoint_id),
                             ("event_type", event_type),
                             ("status", status)] if v is not None}
for k in filters: assert k in _ALLOWED  # guard against future edits
# build clause with numeric placeholders
```

---

### HI-06: `retry_delivery` wrapping `fetchrow` row into new `Event` may drop `target_roles`

**File:** `app/webhooks/router.py:213-219`
**Severity:** High
**Category:** bug

**Issue:** When a failed delivery is retried, the reconstructed `Event` has `target_roles=[]` (default) even if the original event had `target_roles=["planner","admin"]`. Today this doesn't matter because retry goes directly to `retry_single_delivery` and bypasses the bus (and therefore the broadcaster). But the code path `event_bus.publish(event)` is one edit away and a future maintainer seeing "retry sends the original event" will assume roles are preserved. The stored `payload` JSON also doesn't contain `target_roles`, so there's no way to reconstruct them faithfully.

Also: `payload_data.get("data", payload_data)` — if the row's payload is a JSONB dict already, `isinstance(row["payload"], str)` is False and we use the dict directly; but older rows (from when this shape changed) could trip the `get("data", …)` fallback silently.

**Fix:**

- Add a `target_roles TEXT[]` column to `webhook_delivery` and populate on INSERT, or store the full `Event` payload envelope.
- Document that `retry_single_delivery` is webhook-only, never republishes to broadcaster.

```sql
ALTER TABLE webhook_delivery ADD COLUMN target_roles TEXT[] DEFAULT '{}';
```

---

## Medium Issues

### ME-01: `_deliver` catches all exceptions on HTTP POST and treats them identically

**File:** `app/webhooks/dispatcher.py:113-120`
**Severity:** Medium
**Category:** bug

**Issue:** `except Exception` swallows `httpx.ConnectError`, `httpx.TimeoutException`, `httpx.TooManyRedirects`, SSL errors, cert-mismatch, etc. — all treated as retryable. Non-retryable DNS errors and cert failures will cycle through 3 attempts + 140 s of backoff for zero gain. Also, `CancelledError` is a subclass of `BaseException` in 3.8+ but `Exception` in older versions — on 3.11 we're fine, but the `except Exception` here explicitly *will not* catch `CancelledError`, which is good but worth commenting.

**Fix:** Classify exceptions:

```python
import httpx
_RETRYABLE = (httpx.ConnectError, httpx.TimeoutException,
              httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)

try:
    resp = await client.post(sub["url"], content=body, headers=headers)
except httpx.ConnectError as exc:
    resp_body = f"connect: {exc}"
except httpx.TimeoutException as exc:
    resp_body = f"timeout: {exc}"
except httpx.HTTPError as exc:
    resp_body = f"http: {exc}"
    # Non-retryable (e.g., InvalidURL) — break out of retry loop
    if not isinstance(exc, _RETRYABLE):
        status = "failed"
        break
```

Also 4xx responses (other than 408/429) should probably not be retried — a 400 now is a 400 in 90 seconds.

---

### ME-02: Two-minute-plus `asyncio.sleep(90)` in retry loop ties up a task

**File:** `app/webhooks/dispatcher.py:143`
**Severity:** Medium
**Category:** bug

**Issue:** The task parked on `asyncio.sleep(90)` cannot be cancelled cleanly during graceful shutdown because the lifespan shutdown cancels `bg_tasks` (the *main dispatcher loop*) but **not the per-delivery tasks it spawned via `create_task`**. On container stop, pending deliveries are abruptly killed, their rows stay at `status='failed' attempts=2` forever.

**Fix:**

- Track spawned tasks (see HI-01) and `gather(*tasks, return_exceptions=True)` in `dispatcher_loop`'s `CancelledError` branch with a bounded timeout.
- Move retries to a DB-driven reaper so shutdown only needs to drain in-flight HTTP (≤ 10 s timeout).

---

### ME-03: `ws_router.issue_ws_token` — no refresh tracking, no revocation

**File:** `app/webhooks/ws_router.py:23-41`
**Severity:** Medium
**Category:** security

**Issue:** Short-lived (5 min) tokens are fine, but:

- No `jti` (JWT ID) claim → cannot revoke an issued token before expiry.
- No `iat`/`nbf` → clock-skew attacks on replay are unbounded by issue time.
- `WS_TOKEN_SECRET` shared with no rotation story.
- If a user's session is logged out, outstanding WS tokens remain valid for up to 5 minutes. For an operational backend this is usually acceptable but should be documented.

**Fix:** Add `jti`, `iat`, `nbf`; store `jti` in a small in-memory LRU (or Redis) revocation set on logout.

```python
import uuid
payload = {
    "sub": str(user.user_id),
    "role": user.role_name,
    "entity": user.entity,
    "iat": datetime.now(timezone.utc),
    "nbf": datetime.now(timezone.utc),
    "exp": exp,
    "jti": str(uuid.uuid4()),
}
```

Also require `leeway=0` explicitly on `jwt.decode` to guard against clock-skew default changes across PyJWT versions, and pin `algorithms=["HS256"]` (already done — good).

---

### ME-04: WebSocket token passed in query string — leaks into access logs

**File:** `app/webhooks/ws_router.py:44-47`, all reverse-proxy access logs
**Severity:** Medium
**Category:** security

**Issue:** `?token=<jwt>` appears in NGINX / ALB / CloudFront access logs and in server-side error traces. JWTs have short TTL (5 min) but are still sensitive and can be replayed during that window.

**Fix:** Prefer a WS subprotocol or the `Authorization` header. FastAPI/Starlette supports both:

```python
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Preferred: subprotocol negotiation
    sec_ws_protocol = ws.headers.get("sec-websocket-protocol", "")
    token = None
    if sec_ws_protocol.startswith("bearer,"):
        token = sec_ws_protocol.split(",", 1)[1].strip()
    if not token:
        token = ws.query_params.get("token")  # fallback for clients that can't set protocol
    ...
    await ws.accept(subprotocol="bearer")
```

At minimum, add log-scrubbing middleware or document that reverse-proxy logs must be configured to strip `token=` params.

---

### ME-05: Webhook `secret` stored in plaintext in DB

**File:** `app/db/002_webhooks.sql:7`, `app/webhooks/router.py:45-57`
**Severity:** Medium
**Category:** security

**Issue:** The 32-byte hex secret is stored plaintext in `webhook_endpoint.secret`. A DB read (SQL injection elsewhere, backup leak, read-only DBA) directly yields all webhook signing secrets. Unlike user passwords, these *must* be recoverable to sign outgoing deliveries, so bcrypt is not an option — but they can be encrypted at rest using a key from KMS/Secrets Manager that the app pulls on startup.

**Fix:** Either:

1. Encrypt at rest: `secret_enc BYTEA`, encrypted with a per-deploy KMS key; decrypt in-memory on startup into a cache.
2. Store a reference: `secret_ref TEXT` (e.g., `arn:aws:secretsmanager:…`) and pull from AWS SM at delivery time (cache with TTL).

If neither is feasible short-term, at least ensure `secret` column is excluded from application logging and from `SELECT *` responses — `list_endpoints` correctly elides it, but `retry_delivery` at `router.py:206-209` fetches it inside a Row that is never logged (good). `test_webhook` at line 233 also handles it correctly.

Also: `secrets.token_hex(32)` produces a 64-char hex string (32 bytes of entropy) — fine. `secrets.token_urlsafe(32)` would save 20 bytes on the wire.

---

### ME-06: Signature verification helper exists but HMAC body-text encoding is ambiguous

**File:** `app/webhooks/signer.py:9-11`
**Severity:** Medium
**Category:** bug

**Issue:** `secret.encode()` and `body.encode()` both use Python's default UTF-8 encoding, which is fine. But if a caller ever passes `bytes` as `body` (common for already-serialized JSON), `body.encode()` will raise `AttributeError`. Also, the sign/verify pair works on string input only — but the dispatcher uses `body = json.dumps(...)` which is guaranteed `str`, so today this is fine. A type annotation would help.

**Fix:**

```python
def sign_payload(secret: str, body: str | bytes) -> str:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
```

And make `verify_signature` length-check the prefix before `compare_digest` to avoid leaking length via timing on a completely wrong-shape signature:

```python
def verify_signature(secret, body, signature):
    if not signature or not signature.startswith("sha256="):
        return False
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature)
```

---

### ME-07: `ConnectionManager._next_id` never wraps — effectively unbounded-grow int, but also lock cost

**File:** `app/webhooks/broadcaster.py:37-50`
**Severity:** Medium
**Category:** quality

**Issue:** `_next_id` is a Python `int`, so no overflow, but every `connect`/`disconnect` takes the `_lock`. More importantly, `broadcast` copies the whole connection dict under the lock *and then iterates outside it*. Between the copy and the send, a connection might be `disconnect()`-ed, leaving the send to a closed WS. The `try/except Exception` catches this but logs nothing, so dead-sockets pile up until the next broadcast.

**Fix:**

1. Log `except Exception as e: logger.debug("WS send failed for %d: %s", ws_id, e)` so we have visibility.
2. Use `asyncio.gather(*sends, return_exceptions=True)` to parallelize sends and surface first-error metrics.

---

### ME-08: `retry_single_delivery` creates a fresh `httpx.AsyncClient` per call

**File:** `app/webhooks/dispatcher.py:157-161`
**Severity:** Medium
**Category:** quality / bug

**Issue:** `httpx.AsyncClient` creation is cheap but not free (~5-10ms + TLS warmup). More importantly, it defeats HTTP/2 and TLS session reuse for the retry path. If an operator retries a batch of 50 failed deliveries, each opens a new TLS connection.

**Fix:** Use the shared client owned by `dispatcher_loop`, or lazily create a module-level one:

```python
_retry_client: httpx.AsyncClient | None = None

async def _get_retry_client() -> httpx.AsyncClient:
    global _retry_client
    if _retry_client is None or _retry_client.is_closed:
        _retry_client = httpx.AsyncClient(timeout=10)
    return _retry_client
```

(Lifecycle: close in lifespan shutdown.)

---

## Low Issues

### LO-01: `event_bus.deferred_events()` resets the context var twice on error

**File:** `app/webhooks/event_bus.py:111-123`
**Severity:** Low
**Category:** quality

**Issue:** In the `except BaseException` branch, `_deferred_buffer.reset(token)` is called, then `raise` propagates. In the `else` branch, it's called again. Harmless but asymmetric; a `finally` block expresses intent more clearly.

**Fix:**

```python
buf: list[Event] = []
token = _deferred_buffer.set(buf)
try:
    yield buf
except BaseException:
    buf.clear()  # belt-and-suspenders
    raise
finally:
    _deferred_buffer.reset(token)

# Flush after reset, outside try
for event in buf:
    await event_bus._fan_out(event)
```

Also: if `_fan_out` itself raises (it doesn't currently, but could), events already flushed won't unflush. Acceptable.

---

### LO-02: `EventBus._fan_out` holds the lock across `put_nowait`

**File:** `app/webhooks/event_bus.py:50-58`
**Severity:** Low
**Category:** quality

**Issue:** `put_nowait` is non-blocking, so the lock is only held briefly. But if the subscriber list grows, this serializes all publishes behind one lock. Convert to a read-mostly pattern: snapshot the list under the lock, then iterate outside.

**Fix:**

```python
async def _fan_out(self, event):
    async with self._lock:
        subs = list(self._subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Subscriber queue full, dropping event %s", event.event_id)
```

---

### LO-03: `events.py` imports `Event` but `__init__.py` is empty

**File:** `app/webhooks/__init__.py`, `app/webhooks/events.py:15`
**Severity:** Low
**Category:** quality

**Issue:** `__init__.py` is a single blank line. The prompt suggests `from app.webhooks import events` is the public interface. Exposing the names explicitly in `__init__.py` makes the package surface clearer and helps IDEs:

**Fix:**

```python
# app/webhooks/__init__.py
from . import events
from .event_bus import event_bus, Event, deferred_events

__all__ = ["events", "event_bus", "Event", "deferred_events"]
```

---

### LO-04: `ROLE_EVENT_MAP` doesn't include an event for `role="supplier"` or unknown roles

**File:** `app/webhooks/broadcaster.py:16-22`
**Severity:** Low
**Category:** quality

**Issue:** `_should_receive` returns `False` if `ROLE_EVENT_MAP.get(role, [])` is empty (unless the event-target-roles check matches). But if a user has an unmapped role (e.g., "qc_manager" added later), they will *never* receive a WebSocket event, even one that lists them in `target_roles`.

Looking at `_should_receive`: the logic reads "if `target_roles` is set and role not in it → reject; then check prefix match". The inverse is not present: if `target_roles` *includes* the user, they should receive it regardless of role prefix. Today, the logic will still reject because `any(event.event_type.startswith(p) for p in [])` is `False`.

**Fix:** Add an explicit "target_roles matched" short-circuit:

```python
def _should_receive(self, role, event):
    prefixes = ROLE_EVENT_MAP.get(role, [])
    if "*" in prefixes:
        return True
    if event.target_roles:
        if role in event.target_roles:
            return True          # target explicit — allow
        return False             # target explicit and not me — reject
    return any(event.event_type.startswith(p) for p in prefixes)
```

---

### LO-05: Test-webhook endpoint uses hardcoded `"test-secret"` — real receivers can't verify signature

**File:** `app/webhooks/router.py:240`
**Severity:** Low
**Category:** quality

**Issue:** When `endpoint_id` is not provided, `secret = "test-secret"` is used to HMAC the test payload. Any receiver expecting to verify a test ping will fail because it doesn't know "test-secret". For a test endpoint this is arguably intentional (skip verification on receiver), but it silently produces an invalid signature.

**Fix:** Either document this behavior in `FRONTEND_WEBHOOK_INTEGRATION.md`, or add a response header indicating "test mode, signature is non-authoritative".

---

### LO-06: Inconsistent import style in `router.py`

**File:** `app/webhooks/router.py:10, 14, 220`
**Severity:** Low
**Category:** quality

**Issue:** `from .dispatcher import retry_single_delivery` is imported inside the function body at line 220, while `from .event_bus import Event, event_bus` is top-level. The local import was likely added to break a circular import. Document why, or restructure to avoid the circular dep (e.g., move `retry_single_delivery` to a module that doesn't import from `router`).

**Fix:** Add a comment:

```python
# Imported here to break circular dep: dispatcher imports event_bus, and we
# need retry_single_delivery only on this code path.
from .dispatcher import retry_single_delivery
```

---

## Positive Observations

- HMAC uses `hmac.compare_digest` correctly (constant-time) — **signer.py verify_signature is safe from timing attacks**.
- JWT decode correctly pins `algorithms=["HS256"]` (no `alg=none` bypass).
- The two bug fixes from the prompt are correctly applied and tested in context:
  - `ws_router.py:35` encodes `str(user.user_id)`; `ws_router.py:71` decodes via `int(payload["sub"])`. Compliant with PyJWT 2.12+.
  - `broadcaster.py:81-82` admins with `role_prefixes` containing `"*"` bypass entity scoping, restoring null-entity event reception.
- `deferred_events()` + `ContextVar` cleanly solves the "events in a transaction" problem.
- `retry_single_delivery` correctly bypasses the event bus to avoid double-pushing WebSocket notifications.
- Named event constructors (`events.py`) are a clean interface — services don't touch `Event`/`event_bus` directly.
- Webhook `secret` is never returned by `list_endpoints` — **good**. It is returned on `create_endpoint` (necessary — one-time-show pattern).
- `webhook_subscription (endpoint_id, event_type)` UNIQUE constraint prevents duplicate subscriptions.
- Dispatcher uses a shared `httpx.AsyncClient` within `dispatcher_loop` (connection pooling).
- `require_permission("production", "webhooks", action=...)` on all CRUD endpoints — no unauthenticated path found.
- `_JSONEncoder` in `broadcaster.py` correctly handles `Decimal` and `datetime`/`date` — a common trap.

---

## Summary Table

| ID     | File                                 | Sev      | Category | Issue                                                                 |
|--------|--------------------------------------|----------|----------|-----------------------------------------------------------------------|
| CR-01  | router.py (all), dispatcher.py:114   | Critical | security | SSRF — unvalidated user-provided webhook URLs                         |
| CR-02  | dispatcher.py:93-137                 | Critical | bug      | Pool starvation / unbounded retry tasks hold pool handles             |
| HI-01  | dispatcher.py:57-60                  | High     | bug      | `asyncio.create_task` without strong-ref → GC can kill mid-flight     |
| HI-02  | router.py:189-222                    | High     | bug      | Retry endpoint returns 200 before dispatch — crash-window loss        |
| HI-03  | event_bus.py:50-58                   | High     | bug      | `put_nowait` on full subscriber queue silently drops events           |
| HI-04  | dispatcher.py:22-38, main.py:51-52   | High     | bug      | No recovery on crash; multi-worker safety absent                      |
| HI-05  | router.py:165-186                    | High     | security | f-string SQL assembly in `list_deliveries` — brittle, future-SQLi     |
| HI-06  | router.py:213-219                    | High     | bug      | Reconstructed retry Event drops `target_roles`                        |
| ME-01  | dispatcher.py:113-120                | Medium   | bug      | All exceptions treated as retryable                                   |
| ME-02  | dispatcher.py:143                    | Medium   | bug      | In-task 90s sleep not cancelled on shutdown                           |
| ME-03  | ws_router.py:23-41                   | Medium   | security | JWT lacks `jti`/`iat`; no revocation                                  |
| ME-04  | ws_router.py:44-47                   | Medium   | security | Token in query string leaks to access logs                            |
| ME-05  | 002_webhooks.sql:7, router.py        | Medium   | security | Webhook secret stored plaintext                                       |
| ME-06  | signer.py:9-11                       | Medium   | bug      | `body.encode()` fails on bytes input; no sig-shape guard              |
| ME-07  | broadcaster.py:37-94                 | Medium   | quality  | WS send errors silently dropped; no structured logs                   |
| ME-08  | dispatcher.py:157-161                | Medium   | quality  | Fresh `httpx.AsyncClient` per retry                                   |
| LO-01  | event_bus.py:111-123                 | Low      | quality  | Asymmetric `_deferred_buffer.reset` — prefer `finally`                |
| LO-02  | event_bus.py:50-58                   | Low      | quality  | `_lock` held across iteration (minor)                                 |
| LO-03  | __init__.py                          | Low      | quality  | Empty `__init__.py` — no public API surface                           |
| LO-04  | broadcaster.py:57-64                 | Low      | bug      | `target_roles` alone doesn't admit delivery if prefix mismatch        |
| LO-05  | router.py:240                        | Low      | quality  | Hardcoded `"test-secret"` for URL-mode test                           |
| LO-06  | router.py:220                        | Low      | quality  | Inline import with no comment explaining circular dep                 |

---

## Recommended Fix Order

1. **Before merging to main:** CR-01 (SSRF) and HI-05 (SQL builder hardening) — both low-effort, high-impact.
2. **Before enabling for external webhook targets:** HI-03, HI-04 (durability), ME-05 (secret encryption), ME-04 (token in query string).
3. **Before scaling to >1 worker:** HI-04 (multi-worker doc / refactor), CR-02 (pool sizing / queue refactor).
4. **Nice-to-haves:** All Medium and Low items can ship as follow-ups.

---

_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
