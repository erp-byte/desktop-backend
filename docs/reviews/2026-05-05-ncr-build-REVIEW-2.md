# NCR Build — Verification Pass 2 (confirmative)

**Reviewed:** 2026-05-07
**Depth:** deep (cross-file: router → service → SQL + comparative pass-1 vs pass-2)
**Scope:**
- `app/db/007_ncr.sql`
- `app/db/008_ncr_v2.sql` (new)
- `app/modules/ncr/router.py`
- `app/modules/ncr/schemas.py`
- `app/modules/ncr/services/event_log.py`
- `app/modules/ncr/services/ncr_service.py`

---

## Pass-1 Critical / High fixes verified

### CR-01 — dual-approve endpoint — **CONFIRMED FIXED, with 2 minor regressions**

**What was added.** `POST /{ncr_no}/dual-approve` (router.py:203-219) → `ncr_service.set_dual_approval` (ncr_service.py:648-714). The handler:

1. Validates `approver_user_id != actor_user_id` (line 660).
2. `_assert_active_user(conn, approver_user_id, "dual_approver")` (line 670) — same helper now also used by `submit_disposition` and `reopen_ncr`. Raises `dual_approver_invalid` 403.
3. Loads `raised_by, disposition_by, concurrence_user_id` under `FOR UPDATE` (line 671-677).
4. `_check_status` against `_ALLOW_DUAL_APPROVE_FROM` (line 680).
5. Loops the three forbidden actors and 403s `dual_approver_invalid` (lines 683-693).
6. Updates `dual_approval_by` + `dual_approval_at`; writes event `"ncr_dual_approved"` (string literal at line 706).

**`_ALLOW_DUAL_APPROVE_FROM` analysis.** The frozenset is `{raised, dispositioned, capa_pending, capa_submitted, capa_failed_verification}` (lines 47-50). I traced whether `raised` is reachable: yes — the spec lets the dual-approver be recorded as soon as the NCR exists, even before disposition. There is no concurrer to compare against in that state (`row["concurrence_user_id"]` is NULL, the loop's `if who and ...` short-circuits). Range is correct.

**Verifier-side data flow.** `verify_capa` re-reads `dual_approval_by` from `ncr_record` under `FOR UPDATE` (line 950-958), so the new column populated by `set_dual_approval` is visible in the same transaction model. The closure path at line 1009-1019 sees `dual_approval_by IS NOT NULL` and proceeds. **End-to-end critical-NCR closure unblocked. Confirmed.**

**Minor regressions introduced** (see "New issues" section): event constant not added to `event_log.py`; permission gate is shared with cancel/reopen; reopen still overwrites a previously-recorded `dual_approval_by`.

---

### CR-02 — `delete_capa` state recompute — **CONFIRMED FIXED**

The new logic at ncr_service.py:894-920 walks the latest remaining round and picks status based on its `(verified_at, is_effective)` triple. Tracing the four scenarios you asked about:

| Scenario | Latest after delete | Branch hit | Resulting status | Correct? |
|---|---|---|---|---|
| r1 ineffective + r2 unverified, delete r2 | r1 (verified, ineffective) | line 909 (`is_effective is False`) | `capa_failed_verification` | ✓ |
| r1 ineffective + r2 ineffective + r3 unverified, delete r3 | r2 (verified, ineffective) | line 909 | `capa_failed_verification` | ✓ |
| r1 unverified, delete r1 | none | line 905 (`latest is None`) | `dispositioned` | ✓ |
| r1 verified-effective (NCR closed): delete blocked at line 883 with `already_verified` | n/a | n/a | n/a | ✓ |

**Single-unverified invariant.** I confirmed `submit_capa` (line 734-747) blocks new round when ANY row has `verified_at IS NULL`. Combined with `delete_capa` blocking deletion of verified rows (line 883), there can be **at most one unverified row per NCR at any time**. The "round 2 unverified + round 3 unverified" case is unreachable.

**Defensive comment at line 912-915.** "Latest is verified-effective — NCR should be 'closed' already" — the comment is **accurate**. Reaching this branch requires deleting an unverified row whose latest sibling is verified-effective, but a verified-effective round already moved status to `closed`, and `_ALLOW_CAPA_FROM` excludes `closed`, so `submit_capa` never minted a later unverified round. The branch is genuinely defensive. The fallback `new_status = "closed"` is safe (it's idempotent with the current actual status).

**One tiny nit.** WR-01's recommended tightening (drop the `pending` check from `submit_capa`) was NOT applied (line 734-747 still uses the pending lookup). This is fine post-fix because the lookup itself is now reliably `None` in non-`capa_submitted` states, but the redundancy noted in the original review remains.

---

### HI-01 — `verify_capa` requires latest round — **CONFIRMED FIXED, with one transactional caveat**

The check at ncr_service.py:962-991 issues two queries inside the same `async with conn.transaction()` block (line 948):
- `SELECT MAX(round_no)` (line 963-966) — no row lock.
- `SELECT verified_at, round_no ... FOR UPDATE` (line 967-973) — row-level lock on the target action.

**Concurrent capa-submit between the two reads.** Walking the race:

1. T1 reads `MAX(round_no) = 5`.
2. T2 (concurrent submit_capa) acquires `ncr_record FOR UPDATE` (line 728), inserts round 6, commits. T2 also flips `ncr_record.status` to `'capa_submitted'`.
3. T1's second read fetches the target action.

There are two sub-cases:
- **T1's target_id refers to round 5 (the previously-pending round).** Then `target.round_no = 5` but the new MAX is now 6. T1 still reads `latest_round = 5` (it cached the value pre-T2's insert). The check `target_round == latest_round` passes — but T1 is verifying a round that T2 has just superseded. **However**, T2's `submit_capa` itself blocks if any unverified row exists (line 734-747), so T2 cannot insert round 6 while round 5 is unverified. The race is closed by T2's own pre-flight check. ✓
- **T1's target_id is for some old verified round.** Then `target.verified_at IS NOT NULL` and the check at line 976 fires `already_verified` 409. ✓

The two reads aren't atomic, but the `submit_capa` invariant (no new round while one is pending) prevents the lost-update window. **Acceptable.**

The `details` payload (`target_round`, `latest_round`) at lines 987-990 is exactly what the FE needs to recover. ✓

---

### HI-02 — entity scope — **CONFIRMED FIXED with 1 minor open item**

**Coverage audit (router.py).** I counted 13 `entity_scope=scope` passes — one per handler (list, detail, update_header, submit_disposition, edit_disposition, dual_approve, submit_capa, update_capa, delete_capa, verify_capa, cancel_ncr, reopen_ncr, audit). `raise_ncr` is the sole exception, which is correct — entity is **derived** from `po_header` at insert time (lines 162-170).

**Service-side `_scope_check_ncr`.** Called inside every mutator (12 call sites, lines 470, 516, 602, 669, 726, 803, 873, 949, 1094, 1134, 1194) including `reopen_ncr` and `dual_approve`. ✓

**Migration 008.**
- Adds `entity TEXT` (nullable) to `ncr_record`.
- Backfills only where `transaction_no` resolves to a `po_header` row.
- Idempotent (`ADD COLUMN IF NOT EXISTS`).
- Index `idx_ncr_entity` created.
- File-name ordering 007 → 008 places it after the schema it depends on. ✓

**Walk-in / no-PO entity behaviour.** The reviewer's question is sharp:

- `raise_ncr` does NOT 400 when entity is unresolved (lines 162-170 leave `entity = None` silently). The COA upload path (per pass-1 receipt review) does 400 in the analogous case. **This is a deliberate-looking deviation**: NCRs can be raised on inspection-only or no-PO walk-in receipts where there is no PO to anchor entity. **However**, the consequence is that those NULL-entity NCRs are then **invisible to non-admin readers** because `list_ncrs` line 294-300 builds `entity = ANY($N::text[])` which excludes NULLs. Pre-fix all rows were visible to all readers (no scope at all); post-fix NULL-entity rows are visible only to admins. **This is a soft regression for QC inspectors and viewers** — they will see fewer rows than before for the no-PO case.

  **Severity: Warning (new).** Either coalesce to `user.entity` at raise-time (so the inspector's home entity tags the NCR) or extend the WHERE to `(entity = ANY(...) OR entity IS NULL)` for read paths. The product call is yours — but the current state is an unannounced visibility change.

- `get_detail` (line 368-370) is gentler: only enforces scope when the row's entity is non-NULL. So reading a *known* ncr_no for a NULL-entity row succeeds for any reader, but the row never appears in lists. Inconsistency between list and detail.

---

### HI-03 — reopen approver SoD — **CONFIRMED FIXED, with ONE GAP**

The four-way check at lines 1148-1158 covers raiser, dispositioner, concurrer + the actor (line 1126). **Missing**: the previously-recorded `dual_approval_by`. If an NCR has been reopened once (writing `dual_approval_by = X`) and then closed and reopened again, the second reopen does NOT block `dual_approver_user_id = X`. The same person can sign two consecutive reopens.

**Severity: Warning (new — call it WR-08).** Symmetric with the verify-side check at line 999. Add:
```python
ncr_row_with_prev = await conn.fetchrow(
    "SELECT raised_by, disposition_by, concurrence_user_id, dual_approval_by FROM ncr_record WHERE ncr_no=$1 FOR UPDATE",
    ncr_no,
)
# … include dual_approval_by in the loop
```

The new `set_dual_approval` (line 648) does not have this issue because it only fires once (subsequent calls would 409 on the status check — `_ALLOW_DUAL_APPROVE_FROM` excludes `closed` and `cancelled`).

**Also still open from pass-1**: neither `reopen_ncr` nor `set_dual_approval` checks that the named approver holds `ncr.record.approve` (only that they're an active user). Pass-1 HI-03 explicitly called out the role check. **The fix is partial** — it nailed SoD but left the role check unaddressed. Any active user (e.g. a `viewer`) can be named as dual approver. Severity: Warning.

---

### HI-04 + WR-02 — concurrence validation — **CONFIRMED FIXED**

`submit_disposition` lines 533-549:
- `_assert_active_user(conn, concurrence_str, "concurrence")` — raises with code `"concurrence_invalid"` (label_invalid pattern at line 129).
- `concurrence != raised_by` — raises `concurrence_required`.
- `concurrence != actor_user_id` — raises `concurrence_required`.

**Order check (your specific question).** The active-user check at line 537 runs FIRST. If `actor_user_id == concurrence_user_id` AND that id is an inactive/unknown user, the response is `concurrence_invalid` 403. If the id IS active and equals the actor, the next branch (line 544-549) fires `concurrence_required` 403. **Both paths are correct 403s; no fall-through to "skipping" the actor check.** ✓

**Error-code consistency.** Pass-1 noted asymmetry — `reopen_ncr` raises `dual_approver_invalid` while disposition raises `concurrence_invalid`. The codes are now distinct AND consistent with the field they describe (`{label}_invalid` from the helper at line 129). FE can switch on the code. ✓

**Role check still missing** (same gap as HI-03): the concurrer's `ncr.record.approve` permission is not verified. Open as a Warning.

---

### HI-05 — verifier full SoD chain — **CONFIRMED FIXED**

Lines 994-1007 loop over `(raised_by, disposition_by, concurrence_user_id, dual_approval_by)`. The `if who and str(who) == actor` guard correctly skips NULL fields, so a minor-severity NCR with `concurrence_user_id IS NULL` (because the spec doesn't require concurrence for minor) and `dual_approval_by IS NULL` (because non-critical) gracefully skips both. ✓

---

## New issues introduced by the fixes

### NEW-01 — `"ncr_dual_approved"` event_type is a string literal — **Warning**

**File:** `app/modules/ncr/services/ncr_service.py:706`
**File:** `app/modules/ncr/services/event_log.py` (constants list)

The new event is written as `event_type="ncr_dual_approved"` while every other event flows through an `event_log.EV_*` constant. The constants block in `event_log.py` (lines 12-26) does NOT include `EV_DUAL_APPROVED`. Side-effects:

- Future audit-event filtering (e.g. an admin UI dropdown sourced from `dir(event_log)`) will silently drop dual-approved events.
- A typo in the literal would produce a corrupt audit row that no `EV_*` lookup catches.
- Downstream consumers (e.g. analytics) that match on the constant set will under-count.

**Fix:** Add `EV_DUAL_APPROVED = "ncr_dual_approved"` to event_log.py and reference it at line 706.

---

### NEW-02 — `reopen_ncr` overwrites a previously-recorded `dual_approval_by` — **Warning**

**File:** `app/modules/ncr/services/ncr_service.py:1160-1177`

The reopen UPDATE writes `dual_approval_by = $4` unconditionally. A typical timeline:

1. Critical NCR raised → `set_dual_approval` records approver A. `dual_approval_by = A`.
2. NCR closes via verify.
3. Reopened by user X with named dual_approver = B. **`dual_approval_by` is overwritten to B.**

The audit log shows event `ncr_dual_approved` with approver A AND `ncr_reopened` with `dual_approver_user_id = B` separately — so the history is recoverable — but `ncr_record.dual_approval_by` now points at B, and a subsequent verify cycle treats B as "the dual approver of record". Whether that's desired is a product call: arguably each lifecycle (raise→close, reopen→close) should have its own dual-approval entry, in which case the current behaviour is correct. But the original CR-01 contract (`dual_approval_by` set ONCE at the second-eyes step, then preserved) is lost.

**Recommendation:** Document the contract explicitly — either (a) keep current behaviour and note "dual_approval_by reflects the most recent reopen-or-set" in the spec, OR (b) clear `dual_approval_by` on close (so each reopen requires a fresh dual_approve), OR (c) keep `dual_approval_by` and don't write it from reopen — reopen has its own separate columns.

I'd argue (c) is cleanest. `reopen_ncr` already writes `reopened_by` and `reopen_reason`; it doesn't need to also overwrite the dual-approval column. **Severity: Warning** (audit clarity, not a security bug).

---

### NEW-03 — `dual-approve` shares `record.approve` with cancel/reopen/disposition — **Info**

**File:** `app/modules/ncr/router.py:206`, `app/db/007_ncr.sql:189-198`

The new endpoint uses `require_permission("ncr", "record", action="approve")`. That gate is the same as `cancel_ncr`, `reopen_ncr`, and `submit_disposition`. Implications:

- Anyone who can cancel can also dual-approve. With the current SoD checks they can't dual-approve their OWN raise/disposition, but they CAN unilaterally clear a critical NCR for closure. `purchase_manager` has `record.approve` (007_ncr.sql:217-224) and would qualify.
- A separate permission like `ncr.record.dual_approve` would let admins keep dispostion broad while restricting dual-approval to a tighter circle (e.g. quality_head). But that's a product decision.

**Severity: Info.** The current grouping is structurally fine for v1 — flag for product review when separate-of-duties policy is finalized.

---

### NEW-04 — `DualApproveRequest.approver_user_id` has no UUID/int format check — **Info**

**File:** `app/modules/ncr/schemas.py:258`

Pydantic field is `str = Field(min_length=1)`. Same shape as `NcrReopenRequest.dual_approver_user_id`. Auth IDs in this codebase are integers cast to string. A request body sending `"haha"` would pass schema validation, then fail at `_assert_active_user`'s `WHERE user_id::text = $1` lookup with a 403 `dual_approver_invalid`. That's a fine outcome (no crash, sensible error), but a tighter regex (`^\d+$`) would 400 earlier. Defer to FE convention.

---

### NEW-05 — NULL-entity rows now invisible to non-admin readers (regression from pre-fix) — **Warning**

**File:** `app/modules/ncr/services/ncr_service.py:294-300`

Pre-fix: there was zero entity scoping (HI-02). All rows visible to all readers.
Post-fix: rows are filtered by `entity = ANY($N::text[])`. NULL-entity rows are excluded from non-admins.

Migration 008 backfills entity ONLY where `transaction_no` matches a PO. **Standalone NCRs (no transaction_no) and walk-in / no-PO NCRs get `entity = NULL` after backfill.** Combined with the list filter, those rows are now visible to admins only.

This is exactly the visibility change the receipt review flagged in the opposite direction (their CR-02). The intentful fix matches one of:
1. Coalesce to `user.entity` at `raise_ncr` time (an inspector's home entity tags the NCR).
2. Modify the list filter to include NULL: `(entity = ANY(...) OR entity IS NULL)`.
3. Backfill differently — derive entity from inspection_id when transaction_no is absent.

**Severity: Warning.** Without one of these, QC inspectors and viewers will see fewer NCRs than before for any historical no-PO data.

---

## Spot checks

### Pass-1 mediums (warnings) revisited

| Pass-1 ID | Title | Status post-fix |
|---|---|---|
| WR-01 | `submit_capa` redundant `pending` check | Still present (line 734-747). Harmless because `delete_capa` recompute (CR-02 fix) preserves the `status='capa_submitted' ⇒ pending exists` invariant. **No-op now**, but fix would simplify. |
| WR-02 | concurrence ≠ actor | **Fixed** (lines 544-549). |
| WR-03 | reopen leaves disposition fields populated | **Not addressed** in pass-2. Still produces stale-disposition trails on reopen→cancel. Severity unchanged. |
| WR-04 | `_CAPA_EDITABLE_COLUMNS` non-determinism | **Fixed** — switched to a sorted tuple at line 791-794. ✓ |
| WR-05 | `_infer_severity(rejected_qty, ...)` dead arg | **Fixed** — line 92 signature now takes only `parameter_severities`. ✓ |
| WR-06 | `verify_capa` plumbs unused `actor_is_admin` | **Fixed** — dropped from signature (router.py:281-283 docstring; ncr_service.py:935). ✓ |
| WR-07 | audit pagination | **Not addressed**. Same exposure. |
| IN-03 | `cascade_event` dead param | Still in `cancel_ncr` signature (line 1089), still unused. Acceptable per pass-1 note. |

### Compile + structural checks

- Imports: `DualApproveRequest`, `DualApproveResponse` added to router (lines 22-23), defined in schemas (lines 254-265). ✓
- No dead code from the rewrite: `actor_is_admin` cleanly removed from both router and service. The `WR-06` plumbing left no stub.
- Migration order: 007_ncr.sql creates the table; 008_ncr_v2.sql `ADD COLUMN IF NOT EXISTS`. Alphabetic ordering ensures 008 runs after 007. Idempotent. ✓
- Status frozenset `_ALLOW_DUAL_APPROVE_FROM` is exclusive of `closed` and `cancelled`, which is correct (don't dual-approve a closed/cancelled NCR).
- The `extra="forbid"` asymmetry from pass-1 IN-01 was not addressed (DualApproveRequest does NOT use `extra="forbid"`). Adds slightly to the inconsistency.

### Permission seed alignment

`purchase_manager` is granted `ncr.record.approve` (line 217-224 of 007_ncr.sql) — a single purchase manager can now: raise → disposition (with concurrer) → CAPA → dual_approve → cancel/reopen. Only `verify` is excluded. Combined with the missing role-check on the dual approver (still open from HI-03), this means that two purchase_manager users can collude end-to-end up to and including dual-approval. Verify alone enforces SoD. **Documented this in pass-1 IN-09 — still applicable, slightly amplified by the dual-approve gate sharing.**

---

## Summary

**Pass-1 verdict per finding:**
- CR-01: ✅ Fixed (with NEW-01, NEW-02, NEW-03 follow-ups)
- CR-02: ✅ Fixed (clean — defensive comment is accurate)
- HI-01: ✅ Fixed (race window closed by `submit_capa`'s own invariant)
- HI-02: ✅ Fixed structurally; NEW-05 is the visibility-regression artifact
- HI-03: ⚠️ Partially fixed — SoD added, but role-check and dual_approval_by-on-prior-reopen still open
- HI-04: ✅ Fixed (apart from role-check, same gap as HI-03)
- HI-05: ✅ Fixed (full chain)

**New issues introduced:** 5 (1 Warning each at NEW-01, NEW-02, NEW-05; 2 Info at NEW-03/04).

**Still-open from pass-1:** WR-01 (redundant), WR-03, WR-07, IN-01, IN-03, IN-06, IN-07, IN-08, IN-09.

---

_Reviewed: 2026-05-07_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep (pass-2 confirmative)_
