# NPD Dispatch-Date Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mail the NPD team, sales POC and business head one day before a sample requisition's expected dispatch date, chase NPD and the BH daily once it has passed, and let the BH cancel the request or move the date straight from the mail.

**Architecture:** An in-process `asyncio` loop started in the FastAPI lifespan (the idiom already used by `dispatcher_loop`, `broadcaster_loop` and `promote_reminder_loop`) ticks hourly, resolves two date buckets against the IST day, and claims a row in a new `sample_dispatch_reminder_log` before sending — so a retry, a restart or a second replica cannot double-mail. The two BH actions link into the web app, which opens a reason box or a native date picker and submits to new email-authenticated endpoints, mirroring the existing `POST /email/bh-signoff-reject` flow.

**Tech Stack:** FastAPI · asyncpg · PostgreSQL · pytest + pytest-asyncio · Next.js 16 (App Router) / React 19 / TypeScript / Tailwind

**Spec:** `server_replica/docs/2026-09-04-npd-dispatch-reminders-design.md`

## Global Constraints

- **No new Python dependency.** APScheduler was explicitly rejected; use the existing `asyncio.create_task` loop idiom.
- **`samples/` migrations are hand-applied** (see the header of `072_*.sql`). Every new table/column must be probed via `information_schema` in code, and its absence must degrade to a no-op, never a 500.
- **All dates are IST.** Use a fixed `timezone(timedelta(hours=5, minutes=30))`; India has no DST, and `ZoneInfo("Asia/Kolkata")` needs `tzdata` which is not a dependency.
- **All user-controlled text rendered into mail must pass through `_fmt`** (HTML-escapes; `None`/blank → `—`).
- **Mail is best-effort and must never raise** into a caller or kill the loop.
- **Both new email links are HMAC-signed** via `email_link_token.sign`. Cancel is terminal and must not be forgeable.
- **Branch first.** Both repos sit on unrelated feature branches — `server_replica` on `feat/jobcard-accounting-crud`, `web_replica` on `feat/remove-output-tab`. Create `feat/dispatch-reminders` in each before Task 1; do not commit onto the existing branches.
- **Open statuses** (verbatim): `DRAFT`, `SUBMITTED`, `BH_APPROVED`, `ON_HOLD`, `IN_PRODUCTION`, `PACKING`, `READY_FOR_DISPATCH`, `PARTIALLY_CONVERTED`.
- **Reminder kinds** (verbatim): `DUE_TOMORROW_NPD`, `DUE_TOMORROW_OWNER`, `OVERDUE_NPD`, `OVERDUE_OWNER`.
- Backend tests run as `PYTHONPATH=. .venv/Scripts/python.exe -m pytest <path> -q` from `server_replica/`.

---

## File Structure

**Create**

| File | Responsibility |
|---|---|
| `server_replica/app/db/samples/087_sample_dispatch_reminder_log.sql` | the send-once guard table |
| `server_replica/app/modules/sample/services/dispatch_reminder_service.py` | IST clock, bucket scan, guard claim/release, `scan_and_send`, the loop |
| `server_replica/tests/services/test_dispatch_reminders.py` | Tasks 1–2, 5 coverage |
| `server_replica/tests/services/test_dispatch_reminder_mail.py` | Tasks 3–4 coverage |
| `web_replica/src/app/modules/sample/[id]/_RedateDialog.tsx` | the date-picker dialog |

**Modify**

| File | Change |
|---|---|
| `server_replica/app/modules/sample/services/sample_mail_service.py` | 4 template builders, 2 signed-URL helpers, 2 senders |
| `server_replica/app/modules/sample/services/email_link_token.py` | document the two new bindings |
| `server_replica/app/modules/sample/router.py` | 2 email endpoints |
| `server_replica/app/modules/sample/schemas.py` | 2 request bodies |
| `server_replica/app/main.py` | start the loop |
| `web_replica/src/app/modules/sample/[id]/page.tsx` | `?req_cancel` / `?req_redate` handling |
| `web_replica/src/lib/sample.ts` | 2 client calls |

---

### Task 1: Guard table + claim/release

**Files:**
- Create: `server_replica/app/db/samples/087_sample_dispatch_reminder_log.sql`
- Create: `server_replica/app/modules/sample/services/dispatch_reminder_service.py`
- Test: `server_replica/tests/services/test_dispatch_reminders.py`

**Interfaces:**
- Consumes: `app.core.helpers.new_short_time_id`
- Produces:
  - `IST: timezone` and `ist_today() -> datetime.date`
  - `KIND_DUE_NPD/KIND_DUE_OWNER/KIND_OVERDUE_NPD/KIND_OVERDUE_OWNER: str`
  - `async has_log_table(conn) -> bool`
  - `async claim(conn, req_id: int, kind: str, day: date) -> bool` — True only if this call won the row
  - `async release_overdue(conn, req_id: int) -> None` — deletes that requisition's `OVERDUE_*` rows

- [ ] **Step 1: Write the failing tests**

Create `server_replica/tests/services/test_dispatch_reminders.py`:

```python
"""Send-once guard for the NPD dispatch reminders.

The loop ticks hourly and may run on several replicas at once, so "did this mail
already go out today?" cannot be decided by reading a flag and then writing it —
two ticks would both read "no". The claim is the INSERT itself: the unique index
on (requisition_id, kind, sent_on) picks exactly one winner and only that caller
sends. These tests pin that, plus the hand-applied-migration no-op.

No DB: the connection is a stand-in that enforces the unique constraint in memory.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminders.py
"""
from __future__ import annotations

import asyncio
from datetime import date

import asyncpg
from asyncpg import exceptions as pg

from app.modules.sample.services import dispatch_reminder_service as svc

REQ = 25495623
DAY = date(2026, 9, 4)


class _Conn:
    """Stand-in enforcing UNIQUE(requisition_id, kind, sent_on) and UNIQUE(id)."""

    def __init__(self, *, has_table=True):
        self.has_table = has_table
        self.rows: list[dict] = []
        self.ids: set[int] = set()

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return 1 if self.has_table else None
        if "INSERT INTO sample_dispatch_reminder_log" in query:
            _id, req_id, kind, day = args
            if _id in self.ids:
                raise pg.UniqueViolationError.new(
                    {"C": "23505", "M": "duplicate key",
                     "n": "sample_dispatch_reminder_log_pkey",
                     "t": "sample_dispatch_reminder_log"})
            if any(r["requisition_id"] == req_id and r["kind"] == kind and r["sent_on"] == day
                   for r in self.rows):
                return None                      # ON CONFLICT DO NOTHING
            self.ids.add(_id)
            self.rows.append({"id": _id, "requisition_id": req_id, "kind": kind, "sent_on": day})
            return _id
        raise AssertionError(f"unexpected fetchval: {query[:60]}")

    async def execute(self, query, *args):
        if "DELETE FROM sample_dispatch_reminder_log" in query:
            (req_id,) = args
            self.rows = [r for r in self.rows
                         if not (r["requisition_id"] == req_id and r["kind"].startswith("OVERDUE"))]
            return "DELETE"
        raise AssertionError(f"unexpected execute: {query[:60]}")


def test_first_claim_wins_and_second_loses_same_day():
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is False


def test_next_day_claims_again_so_the_chase_repeats():
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, date(2026, 9, 5))) is True


def test_kinds_are_independent():
    """The NPD copy going out must not suppress the business head's."""
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_OVERDUE_OWNER, DAY)) is True


def test_requisitions_are_independent():
    conn = _Conn()
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_DUE_NPD, DAY)) is True
    assert asyncio.run(svc.claim(conn, 99999999, svc.KIND_DUE_NPD, DAY)) is True


def test_claim_retries_past_an_id_collision(monkeypatch):
    """id is an app-supplied 8-digit time id, not a SERIAL — a collision must retry
    with a fresh id, not be mistaken for 'already sent'."""
    conn = _Conn()
    conn.ids.add(1111)
    seq = iter([1111, 2222])
    monkeypatch.setattr(svc, "new_short_time_id", lambda: next(seq))
    assert asyncio.run(svc.claim(conn, REQ, svc.KIND_DUE_NPD, DAY)) is True
    assert conn.rows[0]["id"] == 2222


def test_unmigrated_reports_no_table():
    conn = _Conn(has_table=False)
    assert asyncio.run(svc.has_log_table(conn)) is False


def test_release_overdue_clears_only_overdue_rows():
    """A redate re-arms the chase against the NEW date; the due-tomorrow history stays."""
    conn = _Conn()
    for k in (svc.KIND_DUE_NPD, svc.KIND_OVERDUE_NPD, svc.KIND_OVERDUE_OWNER):
        asyncio.run(svc.claim(conn, REQ, k, DAY))
    asyncio.run(svc.release_overdue(conn, REQ))
    assert [r["kind"] for r in conn.rows] == [svc.KIND_DUE_NPD]


def test_ist_today_is_ahead_of_utc_across_the_boundary():
    """At 20:00 UTC it is already the next day in IST (+05:30). Using the server's
    date here would put 'tomorrow' on the wrong side for half the working day."""
    from datetime import datetime, timezone as _tz
    utc_evening = datetime(2026, 9, 4, 20, 0, tzinfo=_tz.utc)
    assert utc_evening.astimezone(svc.IST).date() == date(2026, 9, 5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminders.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.modules.sample.services.dispatch_reminder_service'`

- [ ] **Step 3: Write the migration**

Create `server_replica/app/db/samples/087_sample_dispatch_reminder_log.sql`:

```sql
-- 087_sample_dispatch_reminder_log.sql
--- Send-once guard for the NPD dispatch-date reminders.
---
--- The reminder loop ticks hourly and may run on more than one instance, so the
--- decision "has this mail already gone out today?" must not be a read followed by
--- a write — two ticks would both read "not yet". The row IS the claim: the unique
--- index picks one winner per (requisition, kind, day) and only that caller sends.
--- sent_on being part of the key is also what makes the daily chase work — tomorrow
--- is simply a new row, with no separate counter to keep.
---
--- Applied out-of-band like samples 068-086; NOT wired into scripts/migrate.py.
--- Idempotent. Safe to re-run.
---
--- id is an app-supplied 8-digit time-based BIGINT (new_short_time_id +
--- retry-on-collision in dispatch_reminder_service) — the same handle pattern as
--- npd_dev_dispatch.dispatch_id. NOT a SERIAL.
CREATE TABLE IF NOT EXISTS sample_dispatch_reminder_log (
    id              BIGINT PRIMARY KEY,
    requisition_id  BIGINT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
    kind            TEXT   NOT NULL,   -- DUE_TOMORROW_NPD | DUE_TOMORROW_OWNER
                                       -- OVERDUE_NPD      | OVERDUE_OWNER
    sent_on         DATE   NOT NULL,   -- the IST day it was sent
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (requisition_id, kind, sent_on)
);
CREATE INDEX IF NOT EXISTS idx_sample_dispatch_reminder_req
    ON sample_dispatch_reminder_log(requisition_id);
```

- [ ] **Step 4: Write the minimal service**

Create `server_replica/app/modules/sample/services/dispatch_reminder_service.py`:

```python
"""NPD dispatch-date reminders — the scan, the send-once guard, and the loop.

A sample requisition carries an expected_dispatch_date set by BD, and nothing used to
watch it. This warns the NPD team, the sales POC and the business head the day before,
then chases NPD and the BH every day it stays past due until the BH cancels the request
or moves the date.

Design doc: docs/2026-09-04-npd-dispatch-reminders-design.md
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone

import asyncpg

from app.core.helpers import new_short_time_id

logger = logging.getLogger(__name__)

# Fixed +05:30 rather than ZoneInfo("Asia/Kolkata"): India has no DST, so the offset is
# exact, and it avoids depending on system tzdata (absent on Windows without the `tzdata`
# package, which is deliberately not a dependency).
IST = timezone(timedelta(hours=5, minutes=30))

KIND_DUE_NPD = "DUE_TOMORROW_NPD"
KIND_DUE_OWNER = "DUE_TOMORROW_OWNER"
KIND_OVERDUE_NPD = "OVERDUE_NPD"
KIND_OVERDUE_OWNER = "OVERDUE_OWNER"

# A requisition past these has shipped (INTERNALLY_DISPATCHED / GATE_PASS_ISSUED /
# CLOSED) or is dead (BH_REJECTED / CANCELLED). Mirrors OPEN_STATUSES in the web app's
# dashboard/_build.ts — keep the two in step.
OPEN_STATUSES = ("DRAFT", "SUBMITTED", "BH_APPROVED", "ON_HOLD",
                 "IN_PRODUCTION", "PACKING", "READY_FOR_DISPATCH", "PARTIALLY_CONVERTED")


def ist_today() -> date:
    """Today in IST. Every date comparison in this module goes through here."""
    return datetime.now(IST).date()


async def has_log_table(conn) -> bool:
    """Whether migration 087 is applied. samples/ migrations are hand-applied, so an
    unmigrated environment must no-op rather than raise on every tick."""
    return bool(await conn.fetchval(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'sample_dispatch_reminder_log'"))


async def claim(conn, req_id: int, kind: str, day: date) -> bool:
    """Claim the right to send `kind` for `req_id` on `day`. True only for the caller
    that won the row — everyone else (a retried tick, a second replica) gets False and
    must not send. The INSERT *is* the lock; there is no read-then-write to race."""
    for _attempt in range(5):
        cand = new_short_time_id()
        try:
            async with conn.transaction():          # savepoint for the id retry
                got = await conn.fetchval(
                    """INSERT INTO sample_dispatch_reminder_log
                           (id, requisition_id, kind, sent_on)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (requisition_id, kind, sent_on) DO NOTHING
                       RETURNING id""",
                    cand, req_id, kind, day)
            return got is not None
        except asyncpg.UniqueViolationError:
            # The PK collided (not the send-once key, which is handled by ON CONFLICT).
            # Retry with a fresh id — treating this as "already sent" would silently
            # swallow the mail.
            continue
    logger.warning("[dispatch-reminder] could not mint an id for req %s kind %s", req_id, kind)
    return False


async def release_overdue(conn, req_id: int) -> None:
    """Forget this requisition's overdue chase — called when the BH moves the date, so
    the new one earns a fresh warning instead of being silenced by yesterday's rows."""
    await conn.execute(
        "DELETE FROM sample_dispatch_reminder_log "
        " WHERE requisition_id = $1 AND kind LIKE 'OVERDUE%'", req_id)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminders.py -q`
Expected: PASS — 8 passed

- [ ] **Step 6: Commit**

```bash
cd server_replica
git checkout -b feat/dispatch-reminders
git add app/db/samples/087_sample_dispatch_reminder_log.sql \
        app/modules/sample/services/dispatch_reminder_service.py \
        tests/services/test_dispatch_reminders.py
git commit -m "feat(sample): send-once guard for dispatch-date reminders"
```

---

### Task 2: The bucket scan

**Files:**
- Modify: `server_replica/app/modules/sample/services/dispatch_reminder_service.py`
- Test: `server_replica/tests/services/test_dispatch_reminders.py`

**Interfaces:**
- Consumes: `OPEN_STATUSES`, `ist_today` (Task 1)
- Produces: `async due_buckets(conn, today: date) -> dict` returning `{"due_tomorrow": [dict], "overdue": [dict]}`; each row is a full `sample_requisitions` row plus an added `overdue_days: int` (0 for the due-tomorrow bucket).

- [ ] **Step 1: Write the failing tests**

Append to `server_replica/tests/services/test_dispatch_reminders.py`:

```python
# --- the scan ---------------------------------------------------------------

class _ScanConn:
    """Returns canned requisition rows and records the SQL + args it was given."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.queries: list[str] = []
        self.args: tuple = ()

    async def fetch(self, query, *args):
        self.queries.append(query)
        self.args = args
        return self.rows


def _req(**kw):
    base = {"id": REQ, "request_id": REQ, "status": "BH_APPROVED",
            "expected_dispatch_date": DAY + timedelta(days=1), "sample_type": "NPD"}
    base.update(kw)
    return base


def test_scan_asks_only_for_open_requisitions_with_a_date():
    conn = _ScanConn([])
    asyncio.run(svc.due_buckets(conn, DAY))
    sql = conn.queries[0]
    assert "expected_dispatch_date IS NOT NULL" in sql
    assert "deleted_at IS NULL" in sql
    for st in svc.OPEN_STATUSES:
        assert st in sql
    # The five non-chased statuses must not be selectable.
    for st in ("INTERNALLY_DISPATCHED", "GATE_PASS_ISSUED", "CLOSED",
               "BH_REJECTED", "CANCELLED"):
        assert st not in sql


def test_scan_passes_the_ist_day_as_the_comparison_date():
    conn = _ScanConn([])
    asyncio.run(svc.due_buckets(conn, DAY))
    assert conn.args == (DAY,)


def test_due_tomorrow_bucket():
    conn = _ScanConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert [r["id"] for r in out["due_tomorrow"]] == [REQ]
    assert out["overdue"] == []


def test_due_today_is_in_neither_bucket():
    """The warning is D-1 and the chase is D+1; the day itself is deliberately silent."""
    conn = _ScanConn([_req(expected_dispatch_date=DAY)])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert out["due_tomorrow"] == [] and out["overdue"] == []


def test_overdue_bucket_carries_its_age():
    conn = _ScanConn([_req(expected_dispatch_date=DAY - timedelta(days=3))])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert [r["overdue_days"] for r in out["overdue"]] == [3]


def test_far_future_is_in_neither_bucket():
    conn = _ScanConn([_req(expected_dispatch_date=DAY + timedelta(days=9))])
    out = asyncio.run(svc.due_buckets(conn, DAY))
    assert out["due_tomorrow"] == [] and out["overdue"] == []
```

Add `from datetime import timedelta` to the test file's imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminders.py -q -k "scan or bucket or due_today or far_future"`
Expected: FAIL — `AttributeError: module ... has no attribute 'due_buckets'`

- [ ] **Step 3: Implement the scan**

Append to `dispatch_reminder_service.py`:

```python
async def due_buckets(conn, today: date) -> dict:
    """Split the chaseable requisitions into the two buckets, against an IST `today`.

    One query, bucketed in Python: the row set is small (open requisitions with a date),
    and doing it here keeps the boundary rules in one readable place instead of two
    near-identical SQL predicates.
    """
    placeholders = ", ".join(f"'{s}'" for s in OPEN_STATUSES)   # module constants, not input
    rows = await conn.fetch(
        f"""SELECT * FROM sample_requisitions
             WHERE deleted_at IS NULL
               AND expected_dispatch_date IS NOT NULL
               AND status IN ({placeholders})
               AND expected_dispatch_date <= $1 + 1
             ORDER BY expected_dispatch_date, id""", today)
    due, over = [], []
    for r in rows:
        d = dict(r)
        exp = d["expected_dispatch_date"]
        if exp == today + timedelta(days=1):
            d["overdue_days"] = 0
            due.append(d)
        elif exp < today:
            d["overdue_days"] = (today - exp).days
            over.append(d)
        # exp == today falls through: the warning is D-1, the chase D+1.
    return {"due_tomorrow": due, "overdue": over}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminders.py -q`
Expected: PASS — 14 passed

- [ ] **Step 5: Commit**

```bash
cd server_replica
git add app/modules/sample/services/dispatch_reminder_service.py tests/services/test_dispatch_reminders.py
git commit -m "feat(sample): due-tomorrow and overdue bucket scan"
```

---

### Task 3: The four mail templates

**Files:**
- Modify: `server_replica/app/modules/sample/services/sample_mail_service.py`
- Test: `server_replica/tests/services/test_dispatch_reminder_mail.py`

**Interfaces:**
- Consumes: existing `_shell`, `_detail_table`, `_key_figures`, `_pill`, `_buttons`, `_fmt`, `_ACTION_FOOTER`, `_TRAIL_FOOTER`
- Produces:
  - `_due_tomorrow_npd_html(req: dict) -> str`
  - `_due_tomorrow_owner_html(req: dict) -> str`
  - `_overdue_npd_html(req: dict, *, days: int) -> str`
  - `_overdue_owner_html(req: dict, *, days: int, bh_email: str | None) -> str` — buttons only when `bh_email` is given

- [ ] **Step 1: Write the failing tests**

Create `server_replica/tests/services/test_dispatch_reminder_mail.py`:

```python
"""The four dispatch-reminder mail cards.

What matters here is not layout but three invariants: the business head's copy is the
only one carrying action buttons, the destructive one is signed, and every value that
came from a user is escaped before it reaches the HTML.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminder_mail.py
"""
from __future__ import annotations

from datetime import date

from app.modules.sample.services import sample_mail_service as m

REQ = {
    "id": 42, "request_id": 25495623, "sample_type": "NPD",
    "npd_target_name": "Date Powder", "quantity": 3, "customer_name": "BigBasket",
    "expected_dispatch_date": date(2026, 9, 5), "warehouse": "W202",
}
BH = "bh@candorfoods.in"


def test_due_tomorrow_cards_carry_no_buttons():
    """The warning is informational on both copies — nothing to act on yet."""
    for html in (m._due_tomorrow_npd_html(REQ), m._due_tomorrow_owner_html(REQ)):
        assert "req_cancel" not in html and "req_redate" not in html


def test_overdue_npd_card_is_informatory_only():
    html = m._overdue_npd_html(REQ, days=3)
    assert "req_cancel" not in html and "req_redate" not in html
    assert "3 days overdue" in html


def test_overdue_owner_card_carries_both_actions():
    html = m._overdue_owner_html(REQ, days=3, bh_email=BH)
    assert "req_cancel=25495623" in html
    assert "req_redate=25495623" in html
    assert "Cancel request" in html and "Change expected date" in html


def test_action_links_address_the_page_by_its_pk_not_the_request_id():
    """The web route /modules/sample/<id> is keyed on the PK (42) while the query
    carries the 8-digit request_id (25495623) — the split _bh_signoff_reject_url
    already uses. Putting the request_id in the path opens a different requisition,
    or none at all."""
    html = m._overdue_owner_html(REQ, days=3, bh_email=BH)
    assert "/modules/sample/42?req_cancel=25495623" in html
    assert "/modules/sample/42?req_redate=25495623" in html
    assert "/modules/sample/25495623" not in html


def test_overdue_owner_trail_copy_has_the_buttons_stripped():
    """Everyone else on the trail sees the same card without a way to act — the links
    are bound to the BH's address, so a stray click could not work anyway."""
    html = m._overdue_owner_html(REQ, days=3, bh_email=None)
    assert "req_cancel" not in html and "req_redate" not in html


def test_action_links_are_signed():
    html = m._overdue_owner_html(REQ, days=3, bh_email=BH)
    from app.modules.sample.services.email_link_token import sign
    assert f"t={sign('req_cancel', 25495623, BH)}" in html
    assert f"t={sign('req_redate', 25495623, BH)}" in html


def test_one_day_overdue_reads_singular():
    assert "1 day overdue" in m._overdue_npd_html(REQ, days=1)


def test_user_text_is_escaped():
    """Customer names are free text and land in the mail — an unescaped one would
    inject markup into every recipient's inbox."""
    evil = {**REQ, "customer_name": '<script>alert(1)</script>'}
    html = m._due_tomorrow_npd_html(evil)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_every_card_renders_without_a_date():
    """expected_dispatch_date can be absent on a row that reached the builder by a
    path other than the scan; the card must degrade, not raise."""
    bare = {"id": 1, "request_id": 2, "sample_type": "NPD"}
    m._due_tomorrow_npd_html(bare)
    m._due_tomorrow_owner_html(bare)
    m._overdue_npd_html(bare, days=1)
    m._overdue_owner_html(bare, days=1, bh_email=BH)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminder_mail.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute '_due_tomorrow_npd_html'`

- [ ] **Step 3: Add the URL helpers and the four builders**

Insert into `sample_mail_service.py` immediately before the `# ── promote dual-approval gate ──` banner:

```python
# ── dispatch-date reminders ──────────────────────────────────────────────────
# Both links bounce through the WEB APP, not the backend: the business head needs a
# reason box and a real date picker, and only the portal can offer those. Both are
# HMAC-signed — unlike the BH *reject* link, which is deliberately unsigned because
# rejecting is non-escalating. Cancel is terminal, so an unsigned link would let anyone
# who guessed an 8-digit request_id and an address kill a live request.
# Both take the requisition's PK *and* its 8-digit request_id, exactly like
# _bh_signoff_reject_url above: the web route /modules/sample/<id> is keyed on the PK,
# while the query carries the request_id the endpoint authenticates against. Using one
# for both would open a different requisition — or none.
def _req_cancel_url(pk_id, request_id, email: str) -> str:
    from app.modules.sample.services.email_link_token import sign
    web = Settings().WEB_APP_URL.rstrip("/")
    t = sign("req_cancel", request_id, email)
    return (f"{web}/modules/sample/{pk_id}?req_cancel={request_id}"
            f"&email={quote(email)}&t={t}")


def _req_redate_url(pk_id, request_id, email: str) -> str:
    from app.modules.sample.services.email_link_token import sign
    web = Settings().WEB_APP_URL.rstrip("/")
    t = sign("req_redate", request_id, email)
    return (f"{web}/modules/sample/{pk_id}?req_redate={request_id}"
            f"&email={quote(email)}&t={t}")


def _exp_day(req: dict) -> str:
    v = req.get("expected_dispatch_date")
    return str(v)[:10] if v else "—"


def _overdue_label(days: int) -> str:
    return f"{days} day{'' if days == 1 else 's'} overdue"


def _due_tomorrow_npd_html(req: dict) -> str:
    """T1 — the NPD team's day-before warning. Informational: NPD cannot move the date."""
    inner = (
        f'<tr><td style="padding:26px 26px 6px">{_pill("Due tomorrow", "#d97706")}'
        f'<p style="margin:0 0 18px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">'
        f'This sample request is due for dispatch tomorrow, <b>{_exp_day(req)}</b>. '
        'Sharing it so the trial and its output are ready in time.</p>'
        f'{_detail_table(req)}</td></tr>')
    return _shell(hdr="#d97706", eyebrow="DISPATCH DUE TOMORROW",
                  title=_fmt(req.get("request_id")), inner=inner, footer=_TRAIL_FOOTER)


def _due_tomorrow_owner_html(req: dict) -> str:
    """T2 — the same warning to the business head and the sales POC, who CAN move it."""
    inner = (
        f'<tr><td style="padding:26px 26px 6px">{_pill("Due tomorrow", "#d97706")}'
        f'<p style="margin:0 0 18px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">'
        f'The sample request you raised is due for dispatch tomorrow, <b>{_exp_day(req)}</b>. '
        'The NPD team has been notified. If the date needs to move, change it on the '
        'portal before it slips.</p>'
        f'{_detail_table(req)}</td></tr>')
    return _shell(hdr="#d97706", eyebrow="DISPATCH DUE TOMORROW",
                  title=_fmt(req.get("request_id")), inner=inner, footer=_TRAIL_FOOTER)


def _overdue_npd_html(req: dict, *, days: int) -> str:
    """T3 — informatory, as specified: NPD is told, the business head is asked to act."""
    inner = (
        f'<tr><td style="padding:26px 26px 6px">{_pill(_overdue_label(days), "#dc2626")}'
        f'<p style="margin:0 0 18px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">'
        f'This sample request has passed its expected dispatch date of '
        f'<b>{_exp_day(req)}</b> — <b>{_overdue_label(days)}</b>. Its business head has '
        'been asked to cancel it or set a new date.</p>'
        f'{_detail_table(req)}</td></tr>')
    return _shell(hdr="#dc2626", eyebrow="DISPATCH DATE PASSED",
                  title=_fmt(req.get("request_id")), inner=inner, footer=_TRAIL_FOOTER)


def _overdue_owner_html(req: dict, *, days: int, bh_email: str | None) -> str:
    """T4 — the business head's actionable card. With `bh_email` it carries Cancel /
    Change-date bound to THAT address; with None it is the identical card, buttons
    stripped, for the button-less broadcast to the rest of the trail."""
    rid, pk = req.get("request_id"), req.get("id")
    if bh_email:
        intro = (f'The sample request you raised has passed its expected dispatch date of '
                 f'<b>{_exp_day(req)}</b> — <b>{_overdue_label(days)}</b>. Tap '
                 '<b>Change expected date</b> to set a new one, or <b>Cancel request</b> '
                 'to close it with a reason. It will keep reminding you daily until one '
                 'of those happens.')
        pairs = [("Change expected date", _req_redate_url(pk, rid, bh_email), "#2563eb"),
                 ("Cancel request", _req_cancel_url(pk, rid, bh_email), "#dc2626")]
        footer = _ACTION_FOOTER
    else:
        intro = (f'This sample request has passed its expected dispatch date of '
                 f'<b>{_exp_day(req)}</b> — <b>{_overdue_label(days)}</b>. Its business '
                 'head has been asked to cancel it or set a new date.')
        pairs = []
        footer = _TRAIL_FOOTER
    inner = (f'<tr><td style="padding:26px 26px 6px">{_pill(_overdue_label(days), "#dc2626")}'
             f'<p style="margin:0 0 18px;font-size:{_T_LEAD}px;color:{_INK};line-height:1.6">'
             f'{intro}</p>{_detail_table(req)}</td></tr>{_buttons(pairs)}')
    return _shell(hdr="#dc2626", eyebrow="DISPATCH DATE PASSED",
                  title=_fmt(rid), inner=inner, footer=footer)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminder_mail.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
cd server_replica
git add app/modules/sample/services/sample_mail_service.py tests/services/test_dispatch_reminder_mail.py
git commit -m "feat(sample): four dispatch-reminder mail templates"
```

---

### Task 4: The two senders

**Files:**
- Modify: `server_replica/app/modules/sample/services/sample_mail_service.py`
- Modify: `server_replica/app/modules/sample/services/email_link_token.py` (docstring only)
- Test: `server_replica/tests/services/test_dispatch_reminder_mail.py`

**Interfaces:**
- Consumes: `resolve_recipients`, `_thread_key`, `_thread_subject`, `_send`, `_broadcast`, `_email_for_user`, and the four builders from Task 3
- Produces:
  - `async notify_dispatch_due_tomorrow(conn, req: dict, *, audience: str) -> bool`
  - `async notify_dispatch_overdue(conn, req: dict, *, days: int, audience: str) -> bool`
  - `audience` is `"npd"` or `"owner"`; both return `True` when a mail was actually addressed to someone, `False` when there was no recipient (the caller then does not claim the guard row).

- [ ] **Step 1: Write the failing tests**

Append to `test_dispatch_reminder_mail.py`:

```python
# --- senders ----------------------------------------------------------------
import asyncio


class _MailConn:
    pass


def _patch(monkeypatch, *, npd, requestor, poc):
    async def _rec(conn, req):
        return {"to": [requestor] if requestor else list(npd), "cc": [poc] if poc else [],
                "npd": list(npd), "inventory": [], "production": [],
                "requestor": requestor, "sales_poc": poc}
    sent: list[dict] = []
    monkeypatch.setattr(m, "resolve_recipients", _rec)
    monkeypatch.setattr(m, "_send", lambda subj, html, to, **kw: sent.append(
        {"to": list(to), "cc": list(kw.get("cc") or []), "html": html}) or "mid")
    monkeypatch.setattr(m, "_broadcast", lambda subj, html, rec, **kw: sent.append(
        {"to": ["<broadcast>"], "cc": [], "html": html}))
    return sent


def test_npd_audience_goes_to_the_team_pool(monkeypatch):
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor="bh@x.in", poc="poc@x.in")
    ok = asyncio.run(m.notify_dispatch_due_tomorrow(_MailConn(), REQ, audience="npd"))
    assert ok is True
    assert sent[0]["to"] == ["npd@x.in"]


def test_owner_audience_addresses_bh_and_poc_together(monkeypatch):
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor="bh@x.in", poc="poc@x.in")
    ok = asyncio.run(m.notify_dispatch_due_tomorrow(_MailConn(), REQ, audience="owner"))
    assert ok is True
    assert sorted(sent[0]["to"]) == ["bh@x.in", "poc@x.in"]


def test_no_npd_recipient_reports_false_so_the_guard_is_not_claimed(monkeypatch):
    """Claiming the row on a mail that reached nobody would mark it sent forever."""
    sent = _patch(monkeypatch, npd=[], requestor="bh@x.in", poc=None)
    assert asyncio.run(m.notify_dispatch_due_tomorrow(_MailConn(), REQ, audience="npd")) is False
    assert sent == []


def test_no_business_head_reports_false(monkeypatch):
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor=None, poc=None)
    assert asyncio.run(m.notify_dispatch_overdue(
        _MailConn(), REQ, days=2, audience="owner")) is False


def test_overdue_owner_send_carries_the_buttons_and_broadcasts_without(monkeypatch):
    sent = _patch(monkeypatch, npd=["npd@x.in"], requestor="bh@x.in", poc="poc@x.in")
    asyncio.run(m.notify_dispatch_overdue(_MailConn(), REQ, days=2, audience="owner"))
    direct = [s for s in sent if s["to"] != ["<broadcast>"]]
    bcast = [s for s in sent if s["to"] == ["<broadcast>"]]
    assert direct and "req_cancel" in direct[0]["html"]
    assert bcast and "req_cancel" not in bcast[0]["html"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminder_mail.py -q -k "audience or recipient or business_head or broadcasts"`
Expected: FAIL — `AttributeError: module ... has no attribute 'notify_dispatch_due_tomorrow'`

- [ ] **Step 3: Implement the senders**

Append to `sample_mail_service.py`, after `notify_dev_dispatch_email`:

```python
async def notify_dispatch_due_tomorrow(conn, req: dict, *, audience: str) -> bool:
    """Day-before warning. `audience` is "npd" (the team pool) or "owner" (the business
    head + the sales POC, who are the two who can actually move the date).

    Returns True only when the mail was addressed to somebody — the caller uses that to
    decide whether to claim the send-once row, so a mail that reached nobody is retried
    on the next tick instead of being recorded as sent. Best-effort, never raises."""
    rid = req.get("request_id")
    rec = await resolve_recipients(conn, req)
    thread, subject = _thread_key(rid), _thread_subject(rid, req.get("sample_type"))
    if audience == "npd":
        to = list(rec["npd"])
        html = _due_tomorrow_npd_html(req)
    else:
        to = _dedupe([rec.get("requestor"), rec.get("sales_poc")])
        html = _due_tomorrow_owner_html(req)
    if not to:
        logger.warning("[sample-mail] no %s recipient for the due-tomorrow warning on "
                       "req %s — nothing sent", audience, req.get("id"))
        return False
    _send(subject, html, to, in_reply_to=thread)
    return True


async def notify_dispatch_overdue(conn, req: dict, *, days: int, audience: str) -> bool:
    """Daily chase once the date has passed. "npd" is informatory; "owner" carries the
    business head's Cancel / Change-date buttons, with the identical card broadcast
    button-less to the rest of the trail. Same True/False contract as above."""
    rid = req.get("request_id")
    rec = await resolve_recipients(conn, req)
    thread, subject = _thread_key(rid), _thread_subject(rid, req.get("sample_type"))
    if audience == "npd":
        to = list(rec["npd"])
        if not to:
            logger.warning("[sample-mail] no npd recipient for the overdue chase on req %s",
                           req.get("id"))
            return False
        _send(subject, _overdue_npd_html(req, days=days), to, in_reply_to=thread)
        return True
    bh = rec.get("requestor")
    if not bh:
        logger.warning("[sample-mail] req %s has no business-head address — the overdue "
                       "card reaches nobody who can act on it", req.get("id"))
        return False
    _send(subject, _overdue_owner_html(req, days=days, bh_email=bh), [bh], in_reply_to=thread)
    _broadcast(subject, _overdue_owner_html(req, days=days, bh_email=None), rec,
               thread=thread, exclude=[bh])
    return True
```

- [ ] **Step 4: Document the new token bindings**

In `email_link_token.py`, extend the docstring's binding list:

```python
    accept      -> sign("npd", request_id, email)
    approve     -> sign("promote", dev_jc_id, approver_kind, email)
    req_cancel  -> sign("req_cancel", request_id, email)
    req_redate  -> sign("req_redate", request_id, email)
```

and append below it:

```python
The two dispatch-reminder links bounce through the web app like the reject link, but
unlike it they ARE signed: cancelling is terminal and irreversible, so an unsigned link
would let anyone who guessed an 8-digit request_id plus an address kill a live request.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminder_mail.py -q`
Expected: PASS — 14 passed

- [ ] **Step 6: Commit**

```bash
cd server_replica
git add app/modules/sample/services/sample_mail_service.py \
        app/modules/sample/services/email_link_token.py \
        tests/services/test_dispatch_reminder_mail.py
git commit -m "feat(sample): dispatch-reminder senders with no-recipient contract"
```

---

### Task 5: scan_and_send + the loop + lifespan wiring

**Files:**
- Modify: `server_replica/app/modules/sample/services/dispatch_reminder_service.py`
- Modify: `server_replica/app/main.py`
- Test: `server_replica/tests/services/test_dispatch_reminders.py`

**Interfaces:**
- Consumes: `due_buckets`, `claim`, `has_log_table` (Tasks 1–2); `notify_dispatch_due_tomorrow`, `notify_dispatch_overdue` (Task 4)
- Produces:
  - `async scan_and_send(conn, *, today: date, dry_run: bool = False) -> dict[str, int]` — counts keyed by kind
  - `async dispatch_reminder_loop(pool) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `test_dispatch_reminders.py`:

```python
# --- orchestration ----------------------------------------------------------

class _FullConn(_Conn):
    """Guard behaviour from _Conn, plus the scan's fetch and the per-kind row release
    scan_and_send uses to undo a claim whose mail reached nobody. _Conn.execute only
    knows the OVERDUE-prefix delete, so that second form is handled here."""

    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.scan_rows = [dict(r) for r in rows]

    async def fetch(self, query, *args):
        return self.scan_rows

    async def execute(self, query, *args):
        if "kind = $2 AND sent_on = $3" in query:
            req_id, kind, day = args
            self.rows = [r for r in self.rows
                         if not (r["requisition_id"] == req_id and r["kind"] == kind
                                 and r["sent_on"] == day)]
            return "DELETE"
        return await super().execute(query, *args)


def _stub_mail(monkeypatch, *, ok=True):
    calls: list[tuple] = []

    async def _due(conn, req, *, audience):
        calls.append(("due", req["id"], audience)); return ok

    async def _over(conn, req, *, days, audience):
        calls.append(("over", req["id"], audience, days)); return ok

    monkeypatch.setattr(svc, "notify_dispatch_due_tomorrow", _due)
    monkeypatch.setattr(svc, "notify_dispatch_overdue", _over)
    return calls


def test_a_due_request_mails_both_audiences_once(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY + timedelta(days=1))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert sorted(c[2] for c in calls) == ["npd", "owner"]
    assert out[svc.KIND_DUE_NPD] == 1 and out[svc.KIND_DUE_OWNER] == 1


def test_running_twice_in_a_day_sends_once(monkeypatch):
    """The hourly tick must not re-mail. This is the whole point of the guard."""
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert len(calls) == 2          # npd + owner, not four


def test_the_next_day_chases_again(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    asyncio.run(svc.scan_and_send(conn, today=DAY + timedelta(days=1)))
    assert len(calls) == 4


def test_a_failed_send_does_not_consume_the_day(monkeypatch):
    """notify_* returning False means nobody was addressed — it must be retried, so the
    guard row must not survive."""
    _stub_mail(monkeypatch, ok=False)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert conn.rows == []


def test_dry_run_sends_nothing_and_claims_nothing(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))])
    out = asyncio.run(svc.scan_and_send(conn, today=DAY, dry_run=True))
    assert calls == [] and conn.rows == []
    assert out[svc.KIND_OVERDUE_NPD] == 1      # still reports what it WOULD send


def test_unmigrated_sends_nothing(monkeypatch):
    calls = _stub_mail(monkeypatch)
    conn = _FullConn([_req(expected_dispatch_date=DAY - timedelta(days=1))], has_table=False)
    out = asyncio.run(svc.scan_and_send(conn, today=DAY))
    assert calls == [] and out == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminders.py -q -k "scan_and_send or twice or next_day or dry_run or unmigrated_sends or failed_send"`
Expected: FAIL — `AttributeError: module ... has no attribute 'scan_and_send'`

- [ ] **Step 3: Implement scan_and_send and the loop**

Append to `dispatch_reminder_service.py`:

```python
from app.modules.sample.services.sample_mail_service import (      # noqa: E402
    notify_dispatch_due_tomorrow, notify_dispatch_overdue)


async def scan_and_send(conn, *, today: date, dry_run: bool = False) -> dict:
    """Resolve both buckets and send whatever has not gone out today. Returns per-kind
    counts of what was sent (or, under dry_run, what WOULD be).

    The claim comes before the send and is released if the send found no recipient: a row
    left behind for a mail nobody received would mark it sent for the rest of the day.
    """
    if not await has_log_table(conn):
        logger.info("[dispatch-reminder] 087 not applied — nothing to do")
        return {}
    buckets = await due_buckets(conn, today)
    counts = {KIND_DUE_NPD: 0, KIND_DUE_OWNER: 0, KIND_OVERDUE_NPD: 0, KIND_OVERDUE_OWNER: 0}

    async def _one(req, kind, audience, send) -> None:
        if dry_run:
            counts[kind] += 1
            return
        if not await claim(conn, req["id"], kind, today):
            return                                   # already sent today, or lost the race
        try:
            ok = await send()
        except Exception:                            # noqa: BLE001
            logger.exception("[dispatch-reminder] send failed for req %s kind %s",
                             req["id"], kind)
            ok = False
        if ok:
            counts[kind] += 1
        else:
            # Undo the claim so the next tick retries rather than recording a phantom send.
            await conn.execute(
                "DELETE FROM sample_dispatch_reminder_log "
                " WHERE requisition_id = $1 AND kind = $2 AND sent_on = $3",
                req["id"], kind, today)

    for req in buckets["due_tomorrow"]:
        await _one(req, KIND_DUE_NPD, "npd",
                   lambda r=req: notify_dispatch_due_tomorrow(conn, r, audience="npd"))
        await _one(req, KIND_DUE_OWNER, "owner",
                   lambda r=req: notify_dispatch_due_tomorrow(conn, r, audience="owner"))
    for req in buckets["overdue"]:
        d = req["overdue_days"]
        await _one(req, KIND_OVERDUE_NPD, "npd",
                   lambda r=req, d=d: notify_dispatch_overdue(conn, r, days=d, audience="npd"))
        await _one(req, KIND_OVERDUE_OWNER, "owner",
                   lambda r=req, d=d: notify_dispatch_overdue(conn, r, days=d, audience="owner"))
    return counts


async def dispatch_reminder_loop(pool) -> None:
    """In-process background loop: hourly, send the day's dispatch reminders.

    Hourly rather than a single daily alarm because this loop lives and dies with the web
    process — a fixed alarm would be missed outright by a restart at the wrong minute.
    With the send-once guard, ticking often just means "the first tick after the app is up
    on a given day sends, the rest no-op", which turns a restart into a delay instead of a
    silent miss.

    NOTE: like dispatcher_loop / broadcaster_loop / promote_reminder_loop, this only ticks
    under a persistent server (uvicorn/ECS) — NOT on the Lambda/Mangum path. scan_and_send
    is deliberately callable on its own so that deployment can drive it externally.

    Unlike promote_reminder_loop, several instances running at once is SAFE here: the
    guard's unique index decides which one sends.
    """
    tick_s = max(15 * 60, int(os.environ.get("SAMPLE_REMINDER_TICK_MIN", "60")) * 60)
    hour = int(os.environ.get("SAMPLE_REMINDER_HOUR", "7"))
    logger.info("Dispatch reminder loop started (tick=%ds, from %02d:00 IST)", tick_s, hour)
    try:
        while True:
            await asyncio.sleep(tick_s)
            try:
                if os.environ.get("SAMPLE_REMINDER_ENABLED", "1").strip() not in ("1", "true", "True"):
                    continue
                if datetime.now(IST).hour < hour:
                    continue                          # too early to mail anyone
                async with pool.acquire() as conn:
                    counts = await scan_and_send(conn, today=ist_today())
                if any(counts.values()):
                    logger.info("Dispatch reminder: sent %s", counts)
            except Exception:                         # noqa: BLE001 — a bad tick must never kill the loop
                logger.exception("Dispatch reminder loop tick failed")
    except asyncio.CancelledError:
        logger.info("Dispatch reminder loop stopped")
        raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminders.py -q`
Expected: PASS — 20 passed

- [ ] **Step 5: Wire it into the lifespan**

In `app/main.py`, after the `promote_reminder_loop` line inside `lifespan`:

```python
    # Warn on sample requisitions due for dispatch tomorrow, and chase the ones past
    # their date. Same persistent-server caveat as the loops above.
    from app.modules.sample.services.dispatch_reminder_service import dispatch_reminder_loop
    bg_tasks.append(asyncio.create_task(dispatch_reminder_loop(pool)))
```

- [ ] **Step 6: Verify the app still imports and the whole suite passes**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -c "import app.main; print('import ok')"`
Expected: `import ok`

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services -q`
Expected: PASS — all green, no regressions

- [ ] **Step 7: Commit**

```bash
cd server_replica
git add app/modules/sample/services/dispatch_reminder_service.py app/main.py tests/services/test_dispatch_reminders.py
git commit -m "feat(sample): hourly dispatch-reminder loop with dry-run support"
```

---

### Task 6: The two email-authenticated endpoints

**Files:**
- Modify: `server_replica/app/modules/sample/schemas.py`
- Modify: `server_replica/app/modules/sample/router.py`
- Test: `server_replica/tests/services/test_dispatch_reminder_endpoints.py`

**Interfaces:**
- Consumes: `email_link_token.verify`, `requisition_service.cancel_requisition`, `requisition_service.update_requisition`, `dispatch_reminder_service.release_overdue`
- Produces: `POST /api/v1/sample/email/requisition-cancel`, `POST /api/v1/sample/email/requisition-redate`

- [ ] **Step 1: Write the failing tests**

Create `server_replica/tests/services/test_dispatch_reminder_endpoints.py`:

```python
"""Auth on the two dispatch-reminder email actions.

These are PUBLIC endpoints: no session, reachable by anyone with the URL. Cancel is
terminal, so the token check is the only thing between a guessed 8-digit request_id and
a destroyed request. These tests pin the rejections, not the happy path.

Run:  PYTHONPATH=. python -m pytest tests/services/test_dispatch_reminder_endpoints.py
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.sample.services.email_link_token import sign, verify

RID = 25495623
BH = "bh@candorfoods.in"


def test_a_cancel_token_does_not_authorise_a_redate():
    """Distinct bindings — a leaked date-change link must not become a cancel."""
    t = sign("req_redate", RID, BH)
    assert verify(t, "req_redate", RID, BH)
    assert not verify(t, "req_cancel", RID, BH)


def test_a_token_is_bound_to_its_request():
    t = sign("req_cancel", RID, BH)
    assert not verify(t, "req_cancel", 11111111, BH)


def test_a_token_is_bound_to_its_recipient():
    t = sign("req_cancel", RID, BH)
    assert not verify(t, "req_cancel", RID, "someone@else.in")


def test_an_absent_token_is_rejected():
    assert not verify("", "req_cancel", RID, BH)
    assert not verify(None, "req_cancel", RID, BH)


def test_guard_rejects_a_bad_token():
    from app.modules.sample.router import _assert_req_action_token
    with pytest.raises(HTTPException) as e:
        _assert_req_action_token("req_cancel", RID, BH, "deadbeef")
    assert e.value.status_code == 403


def test_guard_rejects_a_blank_email():
    from app.modules.sample.router import _assert_req_action_token
    with pytest.raises(HTTPException) as e:
        _assert_req_action_token("req_cancel", RID, "", sign("req_cancel", RID, ""))
    assert e.value.status_code == 403


def test_guard_accepts_a_good_token():
    from app.modules.sample.router import _assert_req_action_token
    _assert_req_action_token("req_cancel", RID, BH, sign("req_cancel", RID, BH))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminder_endpoints.py -q`
Expected: FAIL — `ImportError: cannot import name '_assert_req_action_token'`

- [ ] **Step 3: Add the schemas**

Append to `schemas.py` after `BhSignoffEmailReject`:

```python
class RequisitionEmailCancel(BaseModel):
    """Cancel a requisition from the overdue-dispatch reminder's Cancel button (087).
    PUBLIC (no session) — authenticated by the signed token bound to
    (req_cancel, request_id, email) plus `email` being that request's business head.
    A reason is required; CANCELLED is terminal."""
    request_id: int
    email: str
    t: str
    reason: str


class RequisitionEmailRedate(BaseModel):
    """Move a requisition's expected dispatch date from the overdue reminder's
    Change-date button (087). Same public-plus-token auth as the cancel above.
    `expected_dispatch_date` is an ISO date (YYYY-MM-DD) — what the portal's native
    <input type="date"> emits."""
    request_id: int
    email: str
    t: str
    expected_dispatch_date: date
```

Ensure `from datetime import date` is imported in `schemas.py`.

- [ ] **Step 4: Add the guard and the two endpoints**

Append to `router.py` after `email_bh_signoff_reject`:

```python
def _assert_req_action_token(action: str, request_id: int, email: str, t: str) -> None:
    """403 unless `t` is this exact (action, request_id, email) signature. Shared by the
    two dispatch-reminder actions. Unlike the BH reject link these ARE signed — cancel is
    terminal, so an unsigned link would let a guessed request_id plus an address kill a
    live request."""
    from app.modules.sample.services.email_link_token import verify
    if not (email or "").strip() or not verify(t, action, request_id, email):
        raise HTTPException(403, detail={"error": "unauthorised",
                                         "message": "This link is not authorised"})


async def _resolve_req_bh(conn, request_id: int, email: str):
    """The auth_user row for this requisition's bound business head, when their address
    matches `email`. None when it does not — the token proves the link was ours, this
    proves the clicker is the person it was issued to."""
    return await conn.fetchrow(
        """SELECT a.user_id, COALESCE(r.role_name, '') AS role_name, sr.id AS req_pk
             FROM sample_requisitions sr
             JOIN auth_user a ON a.user_id = COALESCE(sr.business_head_user_id,
                                                      sr.requestor_user_id)
             LEFT JOIN auth_role r ON a.role_id = r.role_id
            WHERE sr.request_id = $1 AND sr.deleted_at IS NULL
              AND lower(a.email) = lower($2) AND COALESCE(a.is_active, TRUE)
            LIMIT 1""", request_id, email)


@router.post("/email/requisition-cancel")
async def email_requisition_cancel(request: Request, body: schemas.RequisitionEmailCancel):
    """Cancel a requisition from the overdue-dispatch reminder. PUBLIC — signed token +
    the caller being that request's business head. A reason is mandatory."""
    email = (body.email or "").strip()
    reason = (body.reason or "").strip()
    _assert_req_action_token("req_cancel", body.request_id, email, body.t)
    if not reason:
        raise HTTPException(422, detail={"error": "reason_required",
                                         "message": "A reason is required to cancel"})
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await _resolve_req_bh(conn, body.request_id, email)
        if row is None:
            raise HTTPException(403, detail={"error": "not_an_approver",
                                             "message": "This link is not authorised"})
        import types as _t
        user = _t.SimpleNamespace(user_id=row["user_id"], role_name=row["role_name"],
                                  is_admin=False, full_name="email")
        return await requisition_service.cancel_requisition(
            conn, row["req_pk"], reason=reason, user=user)


@router.post("/email/requisition-redate")
async def email_requisition_redate(request: Request, body: schemas.RequisitionEmailRedate):
    """Move a requisition's expected dispatch date from the overdue reminder. Clears the
    overdue reminder rows so the NEW date earns a fresh warning instead of being silenced
    by yesterday's chase."""
    email = (body.email or "").strip()
    _assert_req_action_token("req_redate", body.request_id, email, body.t)
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await _resolve_req_bh(conn, body.request_id, email)
        if row is None:
            raise HTTPException(403, detail={"error": "not_an_approver",
                                             "message": "This link is not authorised"})
        import types as _t
        user = _t.SimpleNamespace(user_id=row["user_id"], role_name=row["role_name"],
                                  is_admin=False, full_name="email")
        out = await requisition_service.update_requisition(
            conn, row["req_pk"],
            payload={"expected_dispatch_date": body.expected_dispatch_date}, user=user)
        from app.modules.sample.services import dispatch_reminder_service as drs
        if await drs.has_log_table(conn):
            await drs.release_overdue(conn, row["req_pk"])
        return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services/test_dispatch_reminder_endpoints.py -q`
Expected: PASS — 7 passed

Run: `cd server_replica && PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/services -q`
Expected: PASS — all green

- [ ] **Step 6: Commit**

```bash
cd server_replica
git add app/modules/sample/schemas.py app/modules/sample/router.py tests/services/test_dispatch_reminder_endpoints.py
git commit -m "feat(sample): signed email endpoints to cancel or redate a requisition"
```

---

### Task 7: The portal dialogs

**Files:**
- Create: `web_replica/src/app/modules/sample/[id]/_RedateDialog.tsx`
- Modify: `web_replica/src/lib/sample.ts`
- Modify: `web_replica/src/app/modules/sample/[id]/page.tsx`

**Interfaces:**
- Consumes: `POST /email/requisition-cancel`, `POST /email/requisition-redate` (Task 6)
- Produces: `cancelRequisitionByEmail(body)`, `redateRequisitionByEmail(body)` in `lib/sample.ts`

- [ ] **Step 1: Add the client calls**

In `web_replica/src/lib/sample.ts`, alongside the existing email-link helpers:

Add directly below `bhSignoffRejectByEmail` (~line 468), copying its shape — full path,
no `BASE` constant, and the exported type is `Requisition`, not `SampleRequisition`:

```ts
// Overdue-dispatch reminder actions (087). The mail's two buttons land on the request
// page with ?req_cancel / ?req_redate plus the signed `t`; the dialogs submit here.
// PUBLIC — authenticated by that token AND `email` being the request's bound business
// head, so both work with no session. Same-origin /api proxy, like the reject above.
export const cancelRequisitionByEmail = (
  requestId: number, email: string, t: string, reason: string,
) => jsonOrThrow<Requisition>(
  post(`/api/v1/sample/email/requisition-cancel`,
       { request_id: requestId, email, t, reason }),
  "Cancel failed");

// `expectedDispatchDate` is the raw YYYY-MM-DD an <input type="date"> emits — passed
// through unparsed, which is exactly what the backend's Optional[date] takes.
export const redateRequisitionByEmail = (
  requestId: number, email: string, t: string, expectedDispatchDate: string,
) => jsonOrThrow<Requisition>(
  post(`/api/v1/sample/email/requisition-redate`,
       { request_id: requestId, email, t, expected_dispatch_date: expectedDispatchDate }),
  "Date change failed");
```

- [ ] **Step 2: Create the date dialog**

Create `web_replica/src/app/modules/sample/[id]/_RedateDialog.tsx`:

```tsx
"use client";

// "Change expected date" from the overdue-dispatch reminder mail. A native
// <input type="date"> deliberately: the business head arrives from an email with no
// portal session and should never have to know the wire format. The control emits
// YYYY-MM-DD, which is exactly what the backend's Optional[date] takes — the same
// pairing the job card's DispatchPlanCard already uses for this field.

import { useState } from "react";

export function RedateDialog({ current, busy, onCancel, onSubmit }: {
  current?: string | null;
  busy?: boolean;
  onCancel: () => void;
  onSubmit: (isoDate: string) => void;
}) {
  const [value, setValue] = useState((current ?? "").slice(0, 10));
  // Today, in the browser's own locale-independent ISO form — a new expected dispatch
  // date in the past would be overdue the moment it was set.
  const today = new Date().toISOString().slice(0, 10);
  const invalid = !value || value < today;
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-end sm:items-center justify-center p-3"
      onClick={() => { if (!busy) onCancel(); }}>
      <div className="bg-white rounded-md w-full max-w-sm p-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-[15px] font-semibold text-[var(--text-primary)] mb-1">Change expected dispatch date</h3>
        <p className="text-[13px] text-[var(--text-secondary)] mb-3">
          The NPD team is notified of the new date, and the daily overdue reminders stop
          until it passes.
        </p>
        <label className="block text-[11px] text-[var(--text-secondary)]">New expected dispatch date
          <input className="form-input mt-0.5" type="date" min={today} autoFocus
            value={value} onChange={(e) => setValue(e.target.value)} />
        </label>
        {value && value < today && (
          <p className="mt-1 text-[12px] text-[var(--aws-error)]">Pick a date in the future.</p>
        )}
        <div className="flex gap-2 mt-4">
          <button disabled={busy} onClick={onCancel}
            className="h-9 px-4 rounded-[2px] border border-[var(--aws-border-strong)] bg-white text-[13px] disabled:opacity-50 hover:bg-[var(--surface-subtle)]">Cancel</button>
          <div className="flex-1" />
          <button disabled={busy || invalid} onClick={() => onSubmit(value)}
            className="h-9 px-5 rounded-[2px] bg-[var(--aws-orange)] text-white text-[13px] font-medium disabled:opacity-50 hover:bg-[var(--aws-orange-hover)]">
            {busy ? "Saving…" : "Save new date"}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Wire both links into the page**

In `web_replica/src/app/modules/sample/[id]/page.tsx`, beside the existing `emailReject` state:

```tsx
  // Set when a dialog was opened from the overdue-dispatch reminder mail
  // (?req_cancel=<request_id>&email=<addr>&t=<token>, or ?req_redate=…). The submit goes
  // through the token-authenticated endpoint — the BH may have no portal session at all.
  const [emailAction, setEmailAction] = useState<
    { kind: "cancel" | "redate"; requestId: number; email: string; t: string } | null>(null);
```

Extend the existing `bh_reject` query-param effect (the one that calls
`history.replaceState`) with the two new params, following its shape exactly:

```tsx
    const cancelId = Number(sp.get("req_cancel") ?? 0);
    const redateId = Number(sp.get("req_redate") ?? 0);
    const tok = sp.get("t") ?? "";
    if ((cancelId > 0 || redateId > 0) && em && tok) {
      window.history.replaceState(null, "", window.location.pathname);
      const kind = cancelId > 0 ? "cancel" : "redate";
      const requestId = cancelId > 0 ? cancelId : redateId;
      queueMicrotask(() => {
        setEmailAction({ kind, requestId, email: em, t: tok });
        setModal(kind === "cancel" ? "cancel" : "redate");
      });
    }
```

Render the date dialog beside the existing modals:

```tsx
      {modal === "redate" && emailAction?.kind === "redate" && (
        <RedateDialog current={req.expected_dispatch_date} busy={busy}
          onCancel={() => { setModal(null); setEmailAction(null); }}
          onSubmit={(isoDate) => run(async () => {
            await redateRequisitionByEmail({
              request_id: emailAction.requestId, email: emailAction.email,
              t: emailAction.t, expected_dispatch_date: isoDate,
            });
            setModal(null); setEmailAction(null);
          })} />
      )}
```

For the cancel path, reuse the page's existing cancel modal by branching its submit.
Replace the `modal === "cancel"` line (~line 424):

```tsx
            if (modal === "cancel") return run(() => cancelRequisition(req.id, data.reason ?? ""));
```

with:

```tsx
            // Arrived from the overdue reminder mail → the token-authenticated endpoint,
            // since the BH may have no portal session. A logged-in cancel still takes the
            // session path. Mirrors how submitReject picks its endpoint on the job card.
            if (modal === "cancel") {
              const reason = data.reason ?? "";
              return run(async () => {
                if (emailAction?.kind === "cancel") {
                  await cancelRequisitionByEmail(
                    emailAction.requestId, emailAction.email, emailAction.t, reason);
                  setEmailAction(null);
                } else {
                  await cancelRequisition(req.id, reason);
                }
              });
            }
```

Import both at the top, adding to the existing `@/lib/sample` import rather than a
second one:

```tsx
import { RedateDialog } from "./_RedateDialog";
// add to the existing sample import:
//   cancelRequisitionByEmail, redateRequisitionByEmail
```

- [ ] **Step 4: Verify the frontend**

Run: `cd web_replica && npx tsc --noEmit`
Expected: no output, exit 0

Run: `cd web_replica && npx eslint "src/app/modules/sample/[id]/page.tsx" "src/app/modules/sample/[id]/_RedateDialog.tsx" src/lib/sample.ts`
Expected: no output, exit 0

Run: `cd web_replica && npx next build`
Expected: `✓ Compiled successfully`

- [ ] **Step 5: Commit**

```bash
cd web_replica
git checkout -b feat/dispatch-reminders
git add "src/app/modules/sample/[id]/page.tsx" "src/app/modules/sample/[id]/_RedateDialog.tsx" src/lib/sample.ts
git commit -m "feat(sample): cancel + change-date dialogs from the dispatch reminder mail"
```

---

## Deployment

1. Apply `087_sample_dispatch_reminder_log.sql` by hand against the live DB (samples/ migrations are not in `scripts/migrate.py`).
2. Deploy the backend with `SAMPLE_REMINDER_ENABLED=0`.
3. Measure the first batch before it lands in anyone's inbox:
   ```python
   await scan_and_send(conn, today=ist_today(), dry_run=True)
   ```
4. Set `SAMPLE_REMINDER_ENABLED=1` in a chosen window. The first tick after 07:00 IST sends.
5. Confirm `SMTP_HOST` and `WEB_APP_URL` are set — with no `WEB_APP_URL` the action buttons point at nothing.
