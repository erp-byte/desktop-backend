---
phase: ncr-build
reviewed: 2026-05-07T00:00:00Z
depth: deep
files_reviewed: 9
files_reviewed_list:
  - app/db/007_ncr.sql
  - app/modules/ncr/__init__.py
  - app/modules/ncr/services/__init__.py
  - app/modules/ncr/schemas.py
  - app/modules/ncr/services/event_log.py
  - app/modules/ncr/services/mint.py
  - app/modules/ncr/services/ncr_service.py
  - app/modules/ncr/router.py
  - app/main.py
findings:
  critical: 2
  high: 5
  warning: 7
  info: 9
  total: 23
status: issues_found
---

# NCR Build — Code Review

**Reviewed:** 2026-05-07
**Depth:** deep (cross-file: router → service → SQL, plus comparative against PO/receipt patterns)
**Status:** issues_found

## Summary

The NCR module implements all 13 documented endpoints with a coherent
state-machine, append-only event log, and per-action permission gates that
mirror the PO and receipt builds. The schema is sound, the transactions are
well-bounded, and the SQL builders avoid string-interpolated user input.

That said, the build has **two genuinely critical defects** in
security-sensitive flows:

1. **Critical-severity NCRs are unclosable on the first cycle.** `verify_capa`
   refuses to close any NCR with `requires_dual_approval=TRUE` until
   `dual_approval_by IS NOT NULL`, but the *only* code path that ever sets
   `dual_approval_by` is `reopen_ncr`. There is no `POST /dual-approve`
   endpoint, no admin override, and `submit_disposition`'s `concurrence_user_id`
   is stored in a different column. A critical NCR therefore stays in
   `capa_submitted` forever.
2. **State-machine integrity bug in `delete_capa`.** Deleting the only
   *unverified* CAPA round when verified rounds remain leaves the NCR's
   `status` as `capa_submitted` even though there is no longer any submitted
   round to verify, allowing the same round number to be re-minted while the
   audit trail shows a phantom "submitted" state.

Five additional high-severity findings cover: NCR list/detail has zero
entity-scope enforcement (PO and receipt both gate on
`auth_role_permission.allowed_entities`); `verify_capa` permits closing a
non-latest CAPA round, leaking newer rounds into a closed NCR; the
verifier-separation-of-duties check ignores `concurrence_user_id`; the
`reopen_ncr` dual-approver validation accepts *any* active user without role
check, making the only dual-approval path security-theatre; and
`concurrence_user_id` on disposition is stored as free-text with no
`auth_user` validation while `dual_approver_user_id` on reopen IS validated.

The remaining items are warnings (state-machine inconsistency on partial CAPA
rollback, partial `extra="forbid"` coverage, a frozenset-iteration
non-determinism affecting audit payloads) and info-level observations
(missing pagination on `/audit`, dead `cascade_event` parameter, etc.).

---

## Critical Issues

### CR-01 — Critical NCRs cannot be closed on first cycle (no `dual_approval_by` setter except reopen)

**Files:**
- `app/modules/ncr/services/ncr_service.py:803-813` (the gate)
- `app/modules/ncr/services/ncr_service.py:912-972` (the *only* setter, in `reopen_ncr`)
- `app/modules/ncr/services/ncr_service.py:111-206` (`raise_ncr` — sets
  `requires_dual_approval=TRUE` but never sets the approver)
- `app/modules/ncr/services/ncr_service.py:442-523` (`submit_disposition` —
  takes `concurrence_user_id` into a *different* column)
- `app/modules/ncr/router.py` (no `/dual-approve` endpoint)

**Issue:** When `severity == "critical"`, `raise_ncr` sets
`requires_dual_approval=TRUE` (line 132). Later, `verify_capa` blocks closure
unless `dual_approval_by` is non-null:

```python
if (
    is_effective
    and ncr["requires_dual_approval"]
    and not ncr["dual_approval_by"]
):
    raise AuthError("dual_approval_required", ..., 403)
```

Tracing every UPDATE on `ncr_record.dual_approval_by` in the codebase:
- `raise_ncr` — does not touch the column.
- `update_header` — does not touch the column.
- `submit_disposition` — sets `concurrence_user_id`, NOT `dual_approval_by`.
- `edit_disposition` — does not touch the column.
- `submit_capa` / `update_capa` / `delete_capa` — do not touch the column.
- `verify_capa` — does not touch the column (only reads it).
- `cancel_ncr` — does not touch the column.
- **`reopen_ncr` (line 954) — the only writer.**

The state-flow assumption baked into `verify_capa` is therefore "a critical
NCR must have been reopened at some point before closure", which is the
opposite of what the spec describes (dual approval is a normal step in the
forward flow). For an NCR raised as critical, dispositioned, CAPA'd, and
brought to `capa_submitted` for the first time, `dual_approval_by` is `NULL`
and the verifier cannot close — the NCR is locked in `capa_submitted`
indefinitely.

The check is also not bypassed for admin actors (`actor_is_admin` is plumbed
into `verify_capa` but never consulted on lines 803-813), so even root-equivalent
users cannot recover from the deadlock without direct DB access.

**Fix:** Add a dedicated dual-approval endpoint OR carry the second-eyes
identity from disposition into `dual_approval_by`. The cleanest fix:

```python
# Option A — promote concurrence into dual_approval at disposition for criticals
# In submit_disposition, after the existing concurrence check:
if sev == "critical":
    # Critical NCRs use the same person who concurred on disposition as
    # the dual approver of record — separation-of-duties is already
    # enforced (concurrence_user_id != raised_by).
    await conn.execute(
        """
        UPDATE ncr_record SET
            dual_approval_by = $2,
            dual_approval_at = $3
         WHERE ncr_no = $1
        """,
        ncr_no, body["concurrence_user_id"], now,
    )
```

```python
# Option B — add POST /api/v1/ncr/{ncr_no}/dual-approve
# Requires record.approve, validates approver != raised_by AND != disposition_by
# AND != current actor, sets dual_approval_by + dual_approval_at, writes
# event_log.EV_DUAL_APPROVED.
```

Option A is preferred because it eliminates the extra round-trip and aligns
with the spec language ("second pair of eyes"). Whichever is chosen, add a
test that raises a critical NCR end-to-end and asserts closure succeeds
without an intervening reopen.

---

### CR-02 — `delete_capa` leaves status inconsistent when an unverified round is removed but verified rounds remain

**File:** `app/modules/ncr/services/ncr_service.py:710-748`

**Issue:** The status-correction logic only handles the all-rounds-deleted case:

```python
remaining = await conn.fetchval(
    "SELECT COUNT(*) FROM ncr_supplier_action WHERE ncr_no = $1",
    ncr_no,
)
if int(remaining or 0) == 0:
    await conn.execute(
        "UPDATE ncr_record SET status = 'dispositioned' WHERE ncr_no = $1",
        ncr_no,
    )
```

Reproducer:
1. Raise NCR, disposition, file CAPA round 1 → status = `capa_submitted`.
2. Verify round 1 ineffective → status = `capa_failed_verification`.
3. File CAPA round 2 → status = `capa_submitted`, round 2 unverified.
4. **Delete CAPA round 2** (which is unverified, so allowed by line 724).
5. `remaining` = 1 (round 1 still in the table, verified ineffective).
6. The `if remaining == 0` branch is skipped, so status stays `capa_submitted`.

After step 6 the NCR shows `status='capa_submitted'` but no submitted-and-
unverified round actually exists. Downstream effects:
- `submit_capa` will refuse a new round because of the
  `pending and status == "capa_submitted"` check at line 597 — wait, no:
  `pending` is computed as "any unverified row exists", and there is none, so
  `pending` is falsy and the new round IS accepted. The new round then takes
  `next_round = MAX(round_no) + 1 = 2`, **reusing the deleted round number**
  (since round 2 was the deleted row, and round 1 still exists, MAX=1, +1=2).
  Number reuse without an audit linkage to the deleted round.
- `verify_capa` cannot run because no unverified round matches.
- The audit trail shows `EV_CAPA_SUBMITTED round_no=2` followed by
  `EV_CAPA_DELETED round_no=2` followed by another `EV_CAPA_SUBMITTED
  round_no=2`. The two round-2 events are not distinguishable except by
  `action_id`.

**Fix:** When deleting an unverified round, recompute status from the
remaining rows. If there is at least one verified-ineffective round and no
unverified round, status is `capa_failed_verification`. If only verified-
effective rounds remain, status should already be `closed` (delete should
have been blocked anyway). Concrete patch:

```python
remaining_rows = await conn.fetch(
    """
    SELECT verified_at, is_effective FROM ncr_supplier_action
     WHERE ncr_no = $1
     ORDER BY round_no DESC
    """,
    ncr_no,
)
if not remaining_rows:
    new_status = "dispositioned"
elif any(r["verified_at"] is None for r in remaining_rows):
    new_status = "capa_submitted"          # an unverified round still pending
elif all(r["is_effective"] is False for r in remaining_rows):
    new_status = "capa_failed_verification"
else:
    # at least one verified-effective remains: the NCR is already closed
    # (and that round shouldn't be deletable). Don't touch status.
    new_status = None

if new_status is not None:
    await conn.execute(
        "UPDATE ncr_record SET status = $2 WHERE ncr_no = $1",
        ncr_no, new_status,
    )
```

Additionally, consider blocking deletion of round numbers that are not the
*latest* round (i.e. allow only `round_no = MAX(round_no)`); deleting an
intermediate round invalidates the round-number monotonicity used by the
audit log and downstream consumers.

---

## High Severity

### HI-01 — `verify_capa` allows verifying a non-latest CAPA round, closing the NCR while newer rounds exist

**File:** `app/modules/ncr/services/ncr_service.py:773-875`

**Issue:** Although `submit_capa` correctly blocks new rounds while one is
pending verification (line 597), `verify_capa` looks up the action *only* by
`(ncr_no, action_id)` and checks only `verified_at IS NULL`. Combined with
`delete_capa` allowing deletion of *any* unverified round (CR-02), the
following sequence is reachable:

1. Round 1 submitted, status=`capa_submitted`.
2. Round 1 verified ineffective, status=`capa_failed_verification`.
3. Round 2 submitted, status=`capa_submitted`.
4. Round 2 deleted (unverified), status stays `capa_submitted` (per CR-02).
5. Round 3 submitted (gets `round_no=2` re-minted, action_id=N).
6. Verifier calls `/verify` with the round-3 action_id, `is_effective=true`.
7. Closure proceeds: NCR moves to `closed`.

That sequence is the natural consequence of CR-02. Even without CR-02 the
endpoint would let a verifier close on any unverified round in a future
multi-round-pending world (defensive coding).

The fix is to additionally assert the round being verified is the round
referenced by the current `capa_submitted` status — i.e. the latest round:

```python
latest = await conn.fetchval(
    """
    SELECT action_id FROM ncr_supplier_action
     WHERE ncr_no = $1
     ORDER BY round_no DESC
     LIMIT 1
    """,
    ncr_no,
)
if int(latest) != action_id:
    raise AuthError(
        "invalid_capa_target",
        "Only the latest CAPA round may be verified.",
        409,
    )
```

Place this BEFORE the existing `_check_status` so that the 409 fires on a
stale UI more clearly than `invalid_status_transition`.

---

### HI-02 — NCR list and detail endpoints have zero entity-scope enforcement

**Files:**
- `app/modules/ncr/services/ncr_service.py:217-301` (`list_ncrs`)
- `app/modules/ncr/services/ncr_service.py:307-367` (`get_detail`)
- `app/modules/ncr/router.py:64-95`
- `app/db/007_ncr.sql:25-77` (`ncr_record` schema — no `entity` column)

**Issue:** The PO router exposes `_allowed_entities_for(request, user)` and
the receipt router has the analogous helper at
`app/modules/receipt/router.py:71-101`. Both call sites pass
`entity_scope=` into the service layer, which then narrows the SQL `WHERE`
to rows matching the caller's `auth_role_permission.allowed_entities`. NCR
does not. Any non-admin user with `ncr.record.read` sees every NCR in the
database regardless of which entity raised it.

There are two layers to this:
1. **Schema gap:** `ncr_record` has no `entity` column. So even if the
   service layer wanted to filter, it has nothing to filter by. The de facto
   scope vector for NCR is `supplier_id`, but `allowed_entities` is the only
   scope mechanism plumbed through `check_permission`.
2. **Service gap:** `list_ncrs` and `get_detail` do not consult the user's
   permission rows at all — they trust the gate from `require_permission` to
   have already filtered.

The PO build had a documented loophole (CR-02 there: NULL `allowed_entities`
treated as "unrestricted" rather than "no scope set" — which the receipt
helper at line 92 explicitly handles correctly). NCR has no scope enforcement
at all, so the loophole question doesn't even apply.

**Fix:** Either (a) add an `entity TEXT NOT NULL` column to `ncr_record`,
populate it from the inspection or receipt at raise-time, and add the same
`_allowed_entities_for` helper into the NCR router; or (b) document
explicitly in the spec and the migration comment that NCR is intentionally
unscoped (cross-entity by design — e.g. for procurement-wide visibility) and
add this rationale to both `007_ncr.sql` and the router's module docstring.
The choice is a product call, but the *current* state is neither a deliberate
design choice nor a documented constraint — it is silent.

If (a) is chosen, mirror `receipt/router.py:_allowed_entities_for` exactly,
including the `if any(r["allowed_entities"] is None for r in rows): return
None` defense against the PO's CR-02 NULL-loophole. Then thread
`entity_scope` into both `list_ncrs` and `get_detail`.

---

### HI-03 — `reopen_ncr` dual-approver validation accepts any active user without role check

**File:** `app/modules/ncr/services/ncr_service.py:912-972`

**Issue:** The reopen flow is the *only* legitimate path that sets
`dual_approval_by`. The validation it performs is:

```python
if str(dual_approver_user_id) == str(actor_user_id):
    raise AuthError("dual_approver_invalid", ..., 403)

approver = await conn.fetchrow(
    "SELECT 1 FROM auth_user WHERE user_id::text = $1 AND is_active",
    str(dual_approver_user_id),
)
if approver is None:
    raise AuthError("dual_approver_invalid", ..., 403)
```

Two gaps:
1. **No role check.** Any active user — viewer, qc_inspector, even a
   recently-onboarded test account — qualifies. The semantic of "dual
   approval" in a quality module is that the second eyes are competent to
   approve, not merely human. Compare to `submit_disposition`'s concurrence
   check, which has the *same* gap (HI-04 below).
2. **No check that the approver isn't the original raiser or
   dispositioner.** The reopener must differ from the approver, but the
   approver could perfectly well be the raiser of the NCR being reopened.
   That defeats separation-of-duties.

In combination with CR-01 (reopen is the *only* dual-approval setter), this
makes critical-NCR closure security-theatre: a purchase_manager raises a
critical NCR (with their seeded `record.approve`), gets stuck in
`capa_submitted`, asks any colleague to do a one-line reopen with the
purchase_manager themselves listed as the dual approver, and the NCR closes
on the next verify with `dual_approval_by = original_raiser`.

**Fix:** Add explicit cross-checks in `reopen_ncr` after loading the NCR row:

```python
ncr_row = await conn.fetchrow(
    """
    SELECT raised_by, disposition_by, concurrence_user_id
      FROM ncr_record WHERE ncr_no = $1
    """,
    ncr_no,
)
disallowed = {str(x) for x in (
    ncr_row["raised_by"], ncr_row["disposition_by"],
    ncr_row["concurrence_user_id"], actor_user_id,
) if x}
if str(dual_approver_user_id) in disallowed:
    raise AuthError(
        "dual_approver_invalid",
        "Dual approver must differ from the raiser, dispositioner, "
        "concurrence user, and the reopener.",
        403,
    )
```

And require that the approver hold `ncr.record.approve` — query
`auth_role_permission` joined to `auth_user`. This is one extra `fetchval`.

---

### HI-04 — `concurrence_user_id` on disposition is stored as free-text with no `auth_user` validation

**File:** `app/modules/ncr/services/ncr_service.py:442-523`

**Issue:** `submit_disposition` enforces:

```python
if sev in ("major", "critical") and not body.get("concurrence_user_id"):
    raise AuthError("concurrence_required", ...)
if (
    body.get("concurrence_user_id")
    and str(body["concurrence_user_id"]) == str(row["raised_by"])
):
    raise AuthError("concurrence_required", ...)
```

…and writes the value to `ncr_record.concurrence_user_id` (TEXT). It never:
- looks the user up in `auth_user`
- checks `is_active`
- checks the user has a relevant permission
- verifies the user differs from the actor performing the disposition (only
  the raiser is checked)

`reopen_ncr` does verify existence + active. The asymmetry is more than
cosmetic: an attacker with `record.approve` can satisfy the "second pair of
eyes" requirement on a major/critical NCR by typing literally any string —
their own user_id, a coworker's id-on-leave, "admin", or `null` (no, the
falsiness check catches that). The check that concurrence != raiser is the
only real defense, and it's bypassable by any actor who is not also the
raiser.

**Fix:** Mirror `reopen_ncr`'s validation block:

```python
conc = body.get("concurrence_user_id")
if conc:
    ok = await conn.fetchval(
        "SELECT 1 FROM auth_user WHERE user_id::text = $1 AND is_active",
        str(conc),
    )
    if not ok:
        raise AuthError(
            "concurrence_required",
            "Concurrence user is unknown or inactive.",
            403,
        )
    if str(conc) == str(actor_user_id):
        raise AuthError(
            "concurrence_required",
            "Concurrence user must differ from the dispositioner.",
            403,
        )
```

Also add the role check (concurrence user must hold `ncr.record.approve`)
for major/critical only.

---

### HI-05 — Verifier separation-of-duties does not exclude `concurrence_user_id`

**File:** `app/modules/ncr/services/ncr_service.py:789-801`

**Issue:** The check excludes the raiser and the dispositioner:

```python
for who, label in (
    (ncr["raised_by"], "raiser"),
    (ncr["disposition_by"], "dispositioner"),
):
    if who and str(who) == actor:
        raise AuthError(...)
```

But `concurrence_user_id` (the second-eyes on disposition) is **not in the
list**. Combined with HI-04 (concurrence is unvalidated) and CR-01 (dual
approval can only be set via reopen), the practical effect is that the same
person can play *both* second-eyes-on-disposition AND verifier-of-CAPA in a
critical-severity flow. That violates the spec's intent — once you've signed
off on the disposition you've expressed an opinion on whether the CAPA is
adequate.

**Fix:** Extend the loop to include `concurrence_user_id` and (post-CR-01
fix) `dual_approval_by`:

```python
for who, label in (
    (ncr["raised_by"], "raiser"),
    (ncr["disposition_by"], "dispositioner"),
    (ncr["concurrence_user_id"], "concurrence approver"),
    (ncr["dual_approval_by"], "dual approver"),
):
    if who and str(who) == actor:
        raise AuthError(
            "permission_denied",
            f"Verifier cannot be the same person as the {label}.",
            403,
            details={"role": label},
        )
```

The corresponding `SELECT` at line 762 must add `concurrence_user_id` to
the column list (already pulls `dual_approval_by`).

---

## Warnings

### WR-01 — `submit_capa` allows a new round when `pending` is empty but `status='capa_submitted'` (interaction with CR-02)

**File:** `app/modules/ncr/services/ncr_service.py:586-630`

The `pending and row["status"] == "capa_submitted"` guard refuses a new round
only if BOTH conditions hold. Per CR-02, `delete_capa` can leave the system
in a state where `status='capa_submitted'` but no unverified row exists
(`pending` evaluates falsy). `submit_capa` then proceeds, mints a new
`round_no`, and overwrites `status` back to `capa_submitted` via line
627-630. The path is reachable but the resulting round numbering is
correct. The bug is downstream of CR-02, not strictly its own; fixing
CR-02 makes this self-consistent.

**Fix:** Tighten the guard to `if row["status"] == "capa_submitted":` (drop
the `pending` check) — the *status* alone is the source of truth. With CR-02
fixed, status will always be `capa_failed_verification` when a new round is
permitted, never `capa_submitted`.

### WR-02 — `submit_disposition` doesn't validate that `concurrence_user_id` differs from the *actor* (only from the raiser)

**File:** `app/modules/ncr/services/ncr_service.py:471-479`

If the actor performing the disposition is *not* the raiser (e.g. a senior
overrides a junior's draft), the actor can list themselves as concurrence.
The current check only blocks `concurrence == raised_by`. See HI-04 for the
combined fix.

### WR-03 — `reopen_ncr` clears `cancelled_*` and `closed_*` columns but leaves disposition fields populated

**File:** `app/modules/ncr/services/ncr_service.py:946-963`

Reopen sets `status='raised'` so the next call must be a fresh
`submit_disposition`. But the existing `disposition`, `disposition_qty`,
`rationale`, `rtv_*`, `concurrence_user_id`, `disposition_at`,
`disposition_by`, and `financial_impact` columns are not cleared. The
`_check_status` at line 462 will accept the new disposition and overwrite
the columns, but if the user reopens and then *cancels* before a new
disposition, the audit trail shows a cancelled NCR with stale disposition
data attached. UX/data-quality issue, not a vulnerability.

**Fix:** In the reopen UPDATE, NULL out the disposition group:

```sql
disposition         = NULL,
disposition_qty     = NULL,
financial_impact    = NULL,
rationale           = NULL,
rtv_vehicle_no      = NULL,
rtv_lr_no           = NULL,
concurrence_user_id = NULL,
disposition_at      = NULL,
disposition_by      = NULL,
```

### WR-04 — `update_capa` SQL builder iterates a `frozenset`, producing a non-deterministic column order

**File:** `app/modules/ncr/services/ncr_service.py:645-707`

```python
_CAPA_EDITABLE_COLUMNS: frozenset[str] = frozenset({
    "root_cause", "corrective_action", ...
})

for k in _CAPA_EDITABLE_COLUMNS:
    ...
```

The SQL is correctness-preserving (params and updates are built in lockstep
within the same loop). The audit payload `fields_changed` likewise iterates
the same frozenset — also correct, just in non-deterministic order. The
issue is debuggability and snapshot-test stability: identical bodies produce
different audit `fields_changed` arrays across processes.

**Fix:** Switch to `tuple` to lock iteration order:

```python
_CAPA_EDITABLE_COLUMNS: tuple[str, ...] = (
    "root_cause", "corrective_action", "preventive_action",
    "target_date", "responsible_person", "evidence_s3_keys",
)
```

The `in` membership tests elsewhere are unchanged in cost on a 6-element
tuple.

### WR-05 — `_infer_severity` ignores `rejected_qty` despite signature

**File:** `app/modules/ncr/services/ncr_service.py:87-95`

```python
def _infer_severity(rejected_qty: float, parameter_severities: list[str]) -> str:
    rank = {"minor": 1, "major": 2, "critical": 3}
    worst = "major"
    for s in parameter_severities:
        ...
```

`rejected_qty` is in the signature but never read. Either drop the
parameter or apply a quantity-based bump (e.g. >X kg auto-major,
>Y kg auto-critical). At minimum drop it from the signature so callers
don't infer behavior that doesn't exist.

### WR-06 — `verify_capa` plumbs `actor_is_admin` but never uses it

**File:**
- `app/modules/ncr/services/ncr_service.py:754-756`
- `app/modules/ncr/router.py:200`

The router passes `actor_is_admin=user.is_admin` and the service signature
accepts it, but the body never references it. Either use it (e.g. as an
emergency-bypass for CR-01 dual-approval deadlock, with a loud event-log
entry), or drop the parameter to avoid implying a capability that doesn't
exist.

### WR-07 — `audit` endpoint has no pagination and no upper bound on returned rows

**File:**
- `app/modules/ncr/services/ncr_service.py:978-1008`
- `app/modules/ncr/router.py:239-244`

A long CAPA cycle (multiple rounds, each with submit + update + delete +
re-submit + verify) can produce dozens of events. Plus reopen cycles. For a
v1 read-only audit, returning the full list is acceptable, but the
`response_model` is missing entirely and the pagination story is
unspecified. Add `limit/offset` query params with a server-enforced cap
(e.g. `page_size <= 200`) and include a `response_model=list[NcrAuditEvent]`
on the route.

---

## Info

### IN-01 — `extra="forbid"` is applied inconsistently across schemas
**File:** `app/modules/ncr/schemas.py`

`NcrRaiseRequest`, `NcrUpdateRequest`, and `DispositionUpdateRequest` use
`extra="forbid"`. `DispositionRequest`, `CapaSubmitRequest`,
`CapaUpdateRequest`, `VerifyRequest`, `NcrCancelRequest`, and
`NcrReopenRequest` do not (and therefore default to Pydantic's `ignore`).
The asymmetry is unsigned: forward-compat for FE additions on some, strict
on others, with no obvious rationale. Pick one (probably `forbid` given the
audit-sensitive surface) and apply uniformly.

### IN-02 — `disposition_by` is missing from `NcrListItem`
**File:** `app/modules/ncr/schemas.py:64-83`

The list schema includes `raised_by_name` but not `disposition_by`. Detail
view exposes both indirectly (via `verification` etc.). Acceptable for v1
but worth noting since FE filters/sorts by dispositioner are common.

### IN-03 — `cancel_ncr.cascade_event` parameter is dead code
**File:** `app/modules/ncr/services/ncr_service.py:881-909`

No call site outside of NCR module passes `cascade_event`, and the NCR
router never sets it. The user noted this in the prompt as intended for QC
integration that's stubbed. Mark with a `# TODO(qc-integration)` or wrap
in `# noqa: ARG002` and document the contract.

### IN-04 — `mint_ncr_no` allows date-prefix drift across days
**File:** `app/modules/ncr/services/mint.py`

Uniqueness is guaranteed by `nextval`. The date prefix is informational and
the migration comment acknowledges this. Acceptable trade-off — but the
docstring should be reproduced in the migration's comment block too, since
DBA-facing readers won't see the Python file.

### IN-05 — `closure_tat_days NUMERIC` declared without precision/scale
**File:** `app/db/007_ncr.sql:76`

Postgres `NUMERIC` without `(p, s)` is unbounded precision and scale (up to
131,072 digits before, 16,383 after). For a TAT in days that won't exceed
~3 digits, declaring `NUMERIC(8, 2)` is more self-documenting and gives the
type-checker tighter constraints. Acceptable as-is.

### IN-06 — `_action_row_to_verification` aliases display-name to user_id
**File:** `app/modules/ncr/services/ncr_service.py:390-399`

```python
"verified_by_name": row["verified_by"],  # display-name lookup elided
```

`raised_by_name` and `dual_approval_by_name` have the same comment. This is
fine for a v1 stub but the field naming is misleading — consumers
displaying the value verbatim will show user_ids in the UI. Either join
`auth_user` for the display name, or rename the field to `verified_by` and
let the FE do the lookup.

### IN-07 — `event_log.write` is best-effort (swallows exceptions)
**File:** `app/modules/ncr/services/event_log.py:36-51`

The function logs a warning and returns. For an *audit* log this is a
deliberate trade-off (we don't want a logging failure to roll back a state
transition), but it means a corrupted/full event-log table can silently
break audit reconstruction. Add a metrics hook or a structured log key
that monitoring can alert on.

### IN-08 — Free-text `inspection_id` with no FK
**File:** `app/db/007_ncr.sql:32`

Acknowledged by the user — `qc_inspection` doesn't exist yet. Add a CHECK
constraint or a comment declaring the format expectation (`INS-YYYY-MM-DD-
NNNNN` or whatever) so QC integration later can backfill safely.

### IN-09 — `purchase_manager` is seeded with `record.approve` (cancel/reopen) plus full CAPA write
**File:** `app/db/007_ncr.sql:217-224`

The seed grants every NCR perm except `verify`. Combined with HI-03 (any
active user can be named dual approver during reopen), a single
purchase_manager can: raise → approve → CAPA → cancel/reopen → re-CAPA.
The only step they can't do solo is `verify`. That's structurally
acceptable but worth a security-design note in the spec: separation-of-
duties is preserved on the *verify* boundary only, not on the disposition
or reopen boundaries.

---

_Reviewed: 2026-05-07_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
