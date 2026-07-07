# NPD Module Access Control — Design

**Date:** 2026-06-16
**Status:** Approved (matrix + change list confirmed by user)
**Repos:** `web_replica` (primary), `server_replica` (backend audit/harden)

## Goal

Restrict the NPD Development module so it is visible/usable only by the intended
roles, and enforce a per-role action matrix. Enforcement is **frontend UI gating
plus backend verify/harden** (frontend gates alone are bypassable via the API).

## Clarified requirements (user answers)

- **Module visibility:** NPD module visible only to `npd_team`, `business_head`,
  `inventory_manager`, `admin`.
- **NPD "don't give access to sample requisition in that"** → hide the standalone
  **Sample** module (Basis RM / Basis FG / Internal) from NPD members. (Already
  `adminOnly`, so already hidden — verify only.)
- **Business Head can also create** a new NPD sample requisition (in addition to
  view / edit / cancel).
- **Enforcement = frontend UI + verify/harden backend.**
- **Out of scope (user-confirmed):** locking down the shared `/modules/sample/[id]`
  detail page for non-module roles (pre-existing, broader than NPD). The cosmetic
  "Outpass No" vs "Challan No" label stays "Outpass No".

## Target access matrix

| Surface | Admin | NPD team | Business Head | Inventory Mgr | Others |
|---|---|---|---|---|---|
| See / navigate NPD module | ✅ | ✅ | ✅ | ✅ | ❌ |
| NPD request — create | ✅ | ✅ | ✅ | ❌ | ❌ |
| NPD request — view | ✅ | ✅ | ✅ | ✅ (read-only) | ❌ |
| NPD request — edit | ✅ | ❌ | ✅ | ❌ | ❌ |
| NPD request — cancel | ✅ | ❌ | ✅ | ❌ | ❌ |
| NPD request — accept / hold (review) | ✅ | ✅ | ❌ | ❌ | ❌ |
| Develop (start dev JC from request) | ✅ | ✅ | ❌ | ❌ | ❌ |
| Dev job card — create / "+ New" | ✅ | ✅ | ❌ | ❌ | ❌ |
| Dev job card — browse + open (view) | ✅ | ✅ | ✅ (read-only) | ✅ (read-only) | ❌ |
| Dev job card — edit recipe/phases/promote | ✅ | ✅ | ❌ | ❌ | ❌ |
| Promote gate — BH approval | ✅ | ❌ | ✅ (requestor) | ❌ | ❌ |
| Promote gate — Inventory-mgr approval | ✅ | ❌ | ❌ | ✅ | ❌ |
| Download outpass | ✅ | ✅ | ❌ | ❌ | ❌ |
| Standalone Sample module | ✅ | ❌ | ❌ | ❌ | ❌ |

## Already enforced today (verify only, no change)

Verified during exploration:

- BH lacks `canNpd` → no accept/hold, no Develop, no dev-JC create/edit. ✓
- BH has `canApprove` → can edit/cancel a requisition (detail edit gate =
  `caps.canApprove` + status DRAFT/SUBMITTED/BH_REJECTED). ✓
- NPD/TRIAL requisitions do **not** render the issuance `ActionBar`
  (`{!isNpdTrial && <ActionBar/>}`); they show a dedicated NPD-review card gated by
  `canNpd`. So BH never sees Approve/Reject on an NPD request. ✓
- NPD team lacks `canApprove`/`canEdit` → no edit/cancel. ✓
- IM lacks every mutating cap → no action buttons anywhere; sees only the INV_MGR
  promote approval. ✓
- `PromoteGatePanel` already shows each role only its own gate (INV_MGR →
  inventory_manager; REQUESTOR_BH → the requestor user_id; admin → either). ✓
- Standalone Sample **list** (`/modules/sample`) is `adminOnly` (denial banner for
  non-admins). ✓

## New work

### 1. Module visibility + route guard

**Problem:** NPD Development tile is `adminOnly` (admin-only), and the
npd-development pages have **no role guard** — any authenticated user can deep-link
into `/modules/npd-development/*`.

**Change:**
- `src/lib/modules.tsx`: add `allowedRoles?: string[]` to `ModuleItem`. NPD
  Development → `allowedRoles: ["npd_team","business_head","inventory_manager"]`
  (admin implicitly allowed). Remove its `adminOnly`. The `/modules` tile filter
  honors `allowedRoles` (show if admin or user holds any listed role).
- New shared client guard `src/app/modules/npd-development/layout.tsx` (`"use client"`)
  that gates every child route: while mounting, render a spinner; once mounted, if
  the user is authenticated but **not** in the allowed set, redirect to `/modules`
  (and render nothing). Mirrors the existing `mounted` + `useRequireAuth` hydration
  pattern. This single layout covers page.tsx, new, convert/new, trials/new,
  job-cards (+ new, [id], [id]/gate-pass).
- Module-membership predicate uses the multi-role-aware helper (see #3).

### 2. Outpass download gate

**Problem:** The "Download outpass" button (dev-JC detail, CLOSED card) and the
`/job-cards/[id]/gate-pass` route are open to any module visitor.

**Change:**
- Add cap `canOutpass = is("npd_team") || isAdmin` to `SampleCaps`/`sampleCaps`.
- Dev-JC detail: gate the "Download outpass" button on `caps.canOutpass`.
- Gate-pass page: in addition to `useRequireAuth`, redirect away (e.g. back to the
  dev JC) if `!canOutpass`. (Belt-and-suspenders on top of the module layout guard,
  because BH/IM ARE module members but must not reach the outpass.)

### 3. Multi-role correctness (foundational)

**Problem:** `roleNameOf` returns only the **first** role; `sampleCaps.is()` checks
that single role. With multi-role auth live, a `business_head + npd_team` user is
mis-gated.

**Change in `src/lib/sample-roles.ts`:**
- Add `roleNamesOf(me): string[]` — returns **all** role codes (string entries and
  `MeRoleEnvelope.code/role_name`).
- `sampleCaps.is(...names)` → `isAdmin || names.some(n => roleNamesOf(me).includes(n))`.
- Update `PromoteGatePanel`'s `isInvMgr` (dev-JC detail) from
  `roleNameOf(me) === "inventory_manager"` to
  `roleNamesOf(me).includes("inventory_manager")`.
- Keep `roleNameOf` (single, for display) but stop using it for gating decisions.

### 4. Backend verify/harden

Audit `server_replica/app/modules/sample/router.py` + services. Confirm (and fix any
gap in) these route guards:

- Dev-JC create / edit-lines / start / close (request_promote) / phases / promote-approval
  → require `npd_team` (or admin); blocks BH/IM. (`npd_auth` narrows within npd_team.)
- Requisition edit (PATCH) / cancel → BH-capable permission; blocks IM and NPD edit/cancel.
- NPD review (accept/hold) → `npd_team`.
- Promote-approval → INV_MGR (inventory_manager) and REQUESTOR_BH (requestor user_id),
  as already done in `act_promote_approval`.

Report (without breaking consumers) any GET endpoint that over-exposes NPD data to
non-module roles. The outpass is a frontend print page reading `getDevJobCard` (a GET);
no backend mutation, so its gate is frontend-only — acceptable since the data is
already viewable to module members.

## Out of scope

- Shared `/modules/sample/[id]` detail lockdown for non-module roles (pre-existing).
- Any change to the standalone Sample module's own access.
- The "Outpass No" / "Challan No" label (stays "Outpass No").

## Verification

- `tsc --noEmit` (exit 0) + `eslint` on changed web_replica files.
- `py_compile` on changed backend files.
- Manual role-walkthrough against the matrix (admin / npd_team / business_head /
  inventory_manager / a shop-floor role): module visibility, each action's
  presence/absence, outpass button + route, dev-JC read-only for BH/IM.
