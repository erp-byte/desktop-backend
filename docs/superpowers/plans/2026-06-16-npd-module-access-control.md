# NPD Module Access Control — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict the NPD Development module to `{npd_team, business_head, inventory_manager, admin}`, enforce the confirmed per-role action matrix in the UI, gate the outpass download to `{npd_team, admin}`, make capability checks multi-role-aware, and harden the one backend grant gap.

**Architecture:** Frontend gating via the existing `sampleCaps` model (made multi-role-aware) + a new `npd-development/layout.tsx` client route guard + module-tile `allowedRoles`. Backend: one idempotent migration revoking an over-broad grant; the rest is verified-already-correct.

**Tech Stack:** Next.js (App Router, client components), TypeScript, FastAPI/asyncpg, Postgres (hand-applied `app/db/samples/*.sql` migrations).

**Spec:** `server_replica/docs/superpowers/specs/2026-06-16-npd-module-access-control-design.md`

**Verification reality (per project memory `web-replica-verification`):** web_replica has NO unit-test runner. Pure logic is unit-tested via a compile-with-local-tsc + node harness; UI is verified with `tsc --noEmit` + `eslint` + a manual role walkthrough. Backend uses `py_compile`; SQL is idempotent and hand-applied (DDL also given in chat for EC2).

---

## File Structure

**Frontend (web_replica):**
- Modify `src/lib/sample-roles.ts` — multi-role helper + `canSeeNpd`/`canOutpass` caps + `canSeeNpdModule()`.
- Modify `src/lib/modules.tsx` — `allowedRoles?: string[]` on `ModuleItem`; set on NPD Development, drop its `adminOnly`.
- Modify `src/app/modules/page.tsx` — tile visibility filter honors `allowedRoles` (multi-role).
- Create `src/app/modules/npd-development/layout.tsx` — client route guard for the 4 roles.
- Modify `src/app/modules/npd-development/job-cards/[id]/page.tsx` — gate "Download outpass" on `canOutpass`; `PromoteGatePanel` inv-mgr check → multi-role.
- Modify `src/app/modules/npd-development/job-cards/[id]/gate-pass/page.tsx` — redirect off the route if `!canOutpass`.
- Create `scratch/test-sample-roles.mjs` (throwaway harness) — unit-test the pure caps logic.

**Backend (server_replica):**
- Create `app/db/samples/077_npd_team_no_requisition_edit.sql` — revoke `requisition/edit` from `npd_team`.

---

## Task 1: Multi-role-aware caps + new capabilities

**Files:**
- Modify: `web_replica/src/lib/sample-roles.ts`
- Test: `web_replica/scratch/test-sample-roles.mjs`

- [ ] **Step 1: Rewrite `sample-roles.ts` to be multi-role-aware and add `canSeeNpd` + `canOutpass`**

Replace the body from `export interface SampleCaps` to end of file with:

```ts
export interface SampleCaps {
  isAdmin: boolean;
  /** create / edit / submit / cancel a requisition */
  canRequest: boolean;
  /** edit/cancel a request body (business requesting side) — business_head + planner */
  canEdit: boolean;
  /** business-head approve / reject + conversion */
  canApprove: boolean;
  canConvert: boolean;
  /** floor: start-production / mark-packing */
  canProduction: boolean;
  /** inventory: outward / dispatch / mark-ready / inv-verify / gate-pass / close */
  canInventory: boolean;
  /** NPD draft author / promote + dev job cards */
  canNpd: boolean;
  /** may SEE & navigate the NPD Development module */
  canSeeNpd: boolean;
  /** may download the NPD dev-JC outpass (gate pass) */
  canOutpass: boolean;
}

/** All role codes the user holds (string entries + envelope code/role_name). */
export function roleNamesOf(me: MeResponse | null): string[] {
  if (!me) return [];
  const out: string[] = [];
  if (typeof me.role_name === "string" && me.role_name) out.push(me.role_name);
  const roles = Array.isArray(me.roles) ? me.roles : null;
  if (roles) {
    for (const r of roles) {
      const code = typeof r === "string" ? r : (r as MeRoleEnvelope).code ?? (r as MeRoleEnvelope).role_name;
      if (code) out.push(code);
    }
  }
  return out;
}

export function sampleCaps(me: MeResponse | null): SampleCaps {
  const isAdmin = isAdminMe(me);
  const roles = roleNamesOf(me);
  const is = (...names: string[]) => isAdmin || names.some((n) => roles.includes(n));
  return {
    isAdmin,
    canRequest: is("planner", "business_head", "npd_team"),
    canEdit: is("business_head", "planner"),
    canApprove: is("business_head"),
    canConvert: is("business_head"),
    canProduction: is("floor_manager"),
    canInventory: is("inventory_manager"),
    canNpd: is("npd_team"),
    canSeeNpd: is("npd_team", "business_head", "inventory_manager"),
    canOutpass: is("npd_team"),
  };
}

/** Module-tile / route-guard predicate for NPD Development. */
export function canSeeNpdModule(me: MeResponse | null): boolean {
  return sampleCaps(me).canSeeNpd;
}
```

Keep `roleNameOf` (single, used only for display) and `isAdminMe` unchanged above this block.

- [ ] **Step 2: Write the unit harness test**

Create `web_replica/scratch/test-sample-roles.mjs`:

```js
// Throwaway harness for the pure caps logic. Run after compiling sample-roles.ts.
// node scratch/test-sample-roles.mjs   (see Step 3 for the compile command)
import assert from "node:assert";
import { sampleCaps, roleNamesOf, canSeeNpdModule } from "./_sample-roles.mjs";

const me = (...roles) => ({ roles, is_admin: roles.includes("admin") });

// roleNamesOf collects all roles
assert.deepStrictEqual(roleNamesOf(me("business_head", "npd_team")).sort(), ["business_head", "npd_team"]);

// business_head: edit/approve, NOT npd/outpass, but can see module
let c = sampleCaps(me("business_head"));
assert.equal(c.canEdit, true); assert.equal(c.canApprove, true);
assert.equal(c.canNpd, false); assert.equal(c.canOutpass, false);
assert.equal(c.canSeeNpd, true);

// npd_team: npd + outpass + see, NOT approve/edit
c = sampleCaps(me("npd_team"));
assert.equal(c.canNpd, true); assert.equal(c.canOutpass, true);
assert.equal(c.canSeeNpd, true); assert.equal(c.canApprove, false); assert.equal(c.canEdit, false);

// inventory_manager: see module, inventory, NOT outpass/npd/edit
c = sampleCaps(me("inventory_manager"));
assert.equal(c.canSeeNpd, true); assert.equal(c.canInventory, true);
assert.equal(c.canOutpass, false); assert.equal(c.canNpd, false); assert.equal(c.canEdit, false);

// shop-floor role: cannot see the module
c = sampleCaps(me("floor_manager"));
assert.equal(c.canSeeNpd, false); assert.equal(canSeeNpdModule(me("floor_manager")), false);

// admin: everything
c = sampleCaps(me("admin"));
assert.equal(c.canSeeNpd, true); assert.equal(c.canOutpass, true); assert.equal(c.canNpd, true);

// MULTI-ROLE: business_head + npd_team gets the UNION (edit AND npd AND outpass)
c = sampleCaps(me("business_head", "npd_team"));
assert.equal(c.canEdit, true); assert.equal(c.canNpd, true); assert.equal(c.canOutpass, true);

console.log("OK — all sample-roles caps assertions passed");
```

- [ ] **Step 3: Compile the TS to a runnable module and run the harness**

Run (from `web_replica/`):
```bash
npx tsc src/lib/sample-roles.ts --outDir scratch/_caps --module esnext --target es2020 --moduleResolution bundler --skipLibCheck \
  && cp scratch/_caps/sample-roles.js scratch/_sample-roles.mjs \
  && node scratch/test-sample-roles.mjs
```
Expected: `OK — all sample-roles caps assertions passed`
(If `sample-roles.ts` imports types from `./auth`, the emitted JS strips the type-only import — runtime is unaffected. If a value import sneaks in, stub it in the harness.)

- [ ] **Step 4: Type-check + lint**

Run (from `web_replica/`): `npx tsc --noEmit && npx eslint src/lib/sample-roles.ts`
Expected: both exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/lib/sample-roles.ts
git commit -m "feat(sample-roles): multi-role caps + canSeeNpd/canOutpass"
```

---

## Task 2: Module-tile visibility (`allowedRoles`)

**Files:**
- Modify: `web_replica/src/lib/modules.tsx`
- Modify: `web_replica/src/app/modules/page.tsx`

- [ ] **Step 1: Add `allowedRoles` to the `ModuleItem` interface**

In `src/lib/modules.tsx`, inside `interface ModuleItem`, add below `adminOnly?: boolean;`:

```ts
  // When set, the tile/route is visible to admins OR any user holding one of
  // these role codes (and hidden from everyone else). Distinct from adminOnly,
  // which is admin-only. Used by /modules and each module's route guard.
  allowedRoles?: string[];
```

- [ ] **Step 2: Set it on NPD Development and remove its `adminOnly`**

In `src/lib/modules.tsx`, in the NPD Development entry, replace:
```ts
    route: "npd-development",
    implemented: true,
    adminOnly: true,
    Icon: NpdDevIcon,
```
with:
```ts
    route: "npd-development",
    implemented: true,
    allowedRoles: ["npd_team", "business_head", "inventory_manager"],
    Icon: NpdDevIcon,
```

- [ ] **Step 3: Update the `/modules` visibility filter**

In `src/app/modules/page.tsx`, add to the imports near `import { MODULES } from "@/lib/modules";`:
```ts
import { roleNamesOf } from "@/lib/sample-roles";
```
Then replace the line:
```ts
    const visible = me?.is_admin ? MODULES : MODULES.filter((m) => !m.adminOnly);
```
with:
```ts
    const myRoles = roleNamesOf(me);
    const visible = me?.is_admin
      ? MODULES
      : MODULES.filter((m) =>
          !m.adminOnly &&
          (!m.allowedRoles || m.allowedRoles.some((r) => myRoles.includes(r))));
```

- [ ] **Step 4: Type-check + lint**

Run (from `web_replica/`): `npx tsc --noEmit && npx eslint src/lib/modules.tsx "src/app/modules/page.tsx"`
Expected: both exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/lib/modules.tsx src/app/modules/page.tsx
git commit -m "feat(modules): allowedRoles tile visibility; NPD module visible to npd/bh/inv/admin"
```

---

## Task 3: NPD route guard (`npd-development/layout.tsx`)

**Files:**
- Create: `web_replica/src/app/modules/npd-development/layout.tsx`

- [ ] **Step 1: Read the Next.js layout guide (per repo AGENTS.md)**

Run (from `web_replica/`): `ls node_modules/next/dist/docs/ 2>/dev/null && echo "---" && grep -ril "layout" node_modules/next/dist/docs/ | head`
Skim the layout doc if present. Confirm a `"use client"` layout that renders `{children}` is supported (it is in standard App Router). If the modified Next.js differs, fall back to a per-page `useRequireNpdAccess` hook (same logic) on each npd-development page instead of a layout.

- [ ] **Step 2: Create the guard layout**

Create `src/app/modules/npd-development/layout.tsx`:

```tsx
"use client";

// Route guard for the entire NPD Development module. Only npd_team /
// business_head / inventory_manager / admin may enter; everyone else is
// redirected to /modules. Mirrors the per-page `mounted` + useRequireAuth
// hydration pattern so SSR (authed=true, me=null) doesn't flash a redirect.

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useRequireAuth, useMe } from "@/lib/user";
import { canSeeNpdModule } from "@/lib/sample-roles";

export default function NpdDevelopmentLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const authed = useRequireAuth(router.replace);
  const me = useMe();
  const [mounted, setMounted] = useState(false);
  useEffect(() => { queueMicrotask(() => setMounted(true)); }, []);

  const allowed = canSeeNpdModule(me);
  useEffect(() => {
    // useRequireAuth owns the unauthenticated redirect. Once we have the
    // profile, bounce anyone outside the allowed role set.
    if (!mounted || !authed) return;
    if (me !== null && !allowed) router.replace("/modules");
  }, [mounted, authed, me, allowed, router]);

  if (!mounted || !authed || me === null) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", color: "#666", fontFamily: "Arial, sans-serif", fontSize: 13 }}>
        Loading…
      </div>
    );
  }
  if (!allowed) return null; // redirect in flight
  return <>{children}</>;
}
```

- [ ] **Step 3: Type-check + lint**

Run (from `web_replica/`): `npx tsc --noEmit && npx eslint "src/app/modules/npd-development/layout.tsx"`
Expected: both exit 0.

- [ ] **Step 4: Manual nav verification**

Start the app (`npm run dev`), sign in as: (a) a shop-floor role → visiting `/modules/npd-development` redirects to `/modules`, and the tile is absent; (b) inventory_manager / business_head / npd_team / admin → the page loads and the tile shows. (Document results; this is the security-critical check.)

- [ ] **Step 5: Commit**

```bash
git add "src/app/modules/npd-development/layout.tsx"
git commit -m "feat(npd): route guard limiting the module to npd/bh/inv/admin"
```

---

## Task 4: Outpass download gate (`canOutpass`)

**Files:**
- Modify: `web_replica/src/app/modules/npd-development/job-cards/[id]/page.tsx`
- Modify: `web_replica/src/app/modules/npd-development/job-cards/[id]/gate-pass/page.tsx`

- [ ] **Step 1: Gate the "Download outpass" button on `canOutpass`**

In `job-cards/[id]/page.tsx`, the CLOSED Output card renders the button inside `<div className="mt-3">`. Wrap it so it only shows for `caps.canOutpass`. Replace:
```tsx
                {/* Permanent outpass — A4 Delivery Challan + Gate Pass for the FG sample. */}
                <div className="mt-3">
                  <button onClick={openGatePass}
```
with:
```tsx
                {/* Permanent outpass — A4 Delivery Challan + Gate Pass for the FG sample.
                    npd_team + admin only; BH/IM may view the card but not download. */}
                {caps.canOutpass && (
                <div className="mt-3">
                  <button onClick={openGatePass}
```
and close the conditional — replace the button's closing `</div>` that ends that block:
```tsx
                    Download outpass
                  </button>
                </div>
```
with:
```tsx
                    Download outpass
                  </button>
                </div>
                )}
```

- [ ] **Step 2: Guard the gate-pass route on `canOutpass`**

In `job-cards/[id]/gate-pass/page.tsx`, add to the imports:
```tsx
import { useMe } from "@/lib/user";
import { sampleCaps } from "@/lib/sample-roles";
```
(keep the existing `useRequireAuth` import). After `const authed = useRequireAuth(router.replace);` add:
```tsx
  const me = useMe();
  const canOutpass = sampleCaps(me).canOutpass;
```
Then add a redirect effect next to the existing auth effects:
```tsx
  // npd_team + admin only — BH/IM are module members but must not reach the outpass.
  useEffect(() => {
    if (authed && me !== null && !canOutpass) {
      router.replace(`/modules/npd-development/job-cards/${id}`);
    }
  }, [authed, me, canOutpass, router, id]);
```
And block render while denied — change:
```tsx
  if (mounted && !authed) return null;
```
to:
```tsx
  if (mounted && !authed) return null;
  if (mounted && me !== null && !canOutpass) return null;
```

- [ ] **Step 3: Type-check + lint**

Run (from `web_replica/`): `npx tsc --noEmit && npx eslint "src/app/modules/npd-development/job-cards/[id]/page.tsx" "src/app/modules/npd-development/job-cards/[id]/gate-pass/page.tsx"`
Expected: both exit 0.

- [ ] **Step 4: Manual verification**

As business_head and inventory_manager: open a CLOSED dev JC → "Download outpass" button is absent; visiting `/job-cards/<id>/gate-pass` directly redirects back to the JC. As npd_team and admin: button present and the outpass renders.

- [ ] **Step 5: Commit**

```bash
git add "src/app/modules/npd-development/job-cards/[id]/page.tsx" "src/app/modules/npd-development/job-cards/[id]/gate-pass/page.tsx"
git commit -m "feat(npd): restrict outpass download to npd_team + admin"
```

---

## Task 5: Multi-role fix in `PromoteGatePanel`

**Files:**
- Modify: `web_replica/src/app/modules/npd-development/job-cards/[id]/page.tsx`

- [ ] **Step 1: Use the multi-role helper for the inventory-manager check**

In `job-cards/[id]/page.tsx`, update the import:
```tsx
import { sampleCaps, roleNameOf, isAdminMe } from "@/lib/sample-roles";
```
to:
```tsx
import { sampleCaps, roleNamesOf, isAdminMe } from "@/lib/sample-roles";
```
Then in `PromoteGatePanel`, replace:
```tsx
  const isInvMgr = roleNameOf(me) === "inventory_manager";
```
with:
```tsx
  const isInvMgr = roleNamesOf(me).includes("inventory_manager");
```
(If `roleNameOf` is used nowhere else in this file after the change, this also removes the now-unused import — confirm with eslint in Step 2.)

- [ ] **Step 2: Type-check + lint**

Run (from `web_replica/`): `npx tsc --noEmit && npx eslint "src/app/modules/npd-development/job-cards/[id]/page.tsx"`
Expected: both exit 0 (fix an unused-import error if `roleNameOf` is now unused — drop it from the import).

- [ ] **Step 3: Commit**

```bash
git add "src/app/modules/npd-development/job-cards/[id]/page.tsx"
git commit -m "fix(npd): multi-role-aware inventory-manager gate check"
```

---

## Task 6: Backend harden — revoke `requisition/edit` from `npd_team`

> **CONFIRM BEFORE APPLYING:** This makes the matrix's "NPD: no edit/cancel" true at the API layer. Side effect: an NPD user can no longer edit OR cancel a requisition (including their own draft) via the API — they create + submit only. This matches the confirmed matrix; flag to the user if they intended NPD to still edit their own drafts.

**Files:**
- Create: `server_replica/app/db/samples/077_npd_team_no_requisition_edit.sql`

- [ ] **Step 1: Write the migration**

Create `server_replica/app/db/samples/077_npd_team_no_requisition_edit.sql`:

```sql
-- 077_npd_team_no_requisition_edit.sql
-- Matrix: NPD team has all access on NPD requests EXCEPT edit and cancel.
-- The requisition PATCH and cancel routes both gate on ('sample','requisition','edit').
-- npd_team was granted that in 035; revoke it so edit/cancel are blocked at the API
-- (npd_team keeps 'requisition/create' for raising + submitting, and 'npd' for dev work).
-- Idempotent. Safe to re-run.

DELETE FROM auth_role_permission
WHERE role_id = (SELECT role_id FROM auth_role WHERE role_name = 'npd_team')
  AND permission_id = (
    SELECT permission_id FROM auth_permission
    WHERE module = 'sample' AND sub_module = 'requisition'
      AND sub_sub_module IS NULL AND action = 'edit');
```

- [ ] **Step 2: Verify it parses (sanity) and is idempotent**

Run (from `server_replica/`): `python -c "import pathlib; s=pathlib.Path('app/db/samples/077_npd_team_no_requisition_edit.sql').read_text(); assert 'DELETE FROM auth_role_permission' in s and 'npd_team' in s; print('OK')"`
Expected: `OK`. (Actual DB apply is hand-run on EC2 — provide the same SQL in chat per project memory `Migrations are hand-applied`.)

- [ ] **Step 3: Commit**

```bash
git add app/db/samples/077_npd_team_no_requisition_edit.sql
git commit -m "fix(sample-rbac): revoke requisition/edit from npd_team (matrix: NPD no edit/cancel)"
```

---

## Task 7: Backend audit report (verification, no code change)

**Files:** none (documentation/verification task).

- [ ] **Step 1: Re-confirm the mutation guards against the matrix**

Read `server_replica/app/modules/sample/router.py` and confirm:
- All `npd-dev-job-cards/*` mutations gate on `('sample','npd', …)` (or `npd/promote`) → `npd_team`+admin only (BH/IM blocked). ✓ expected.
- `requisitions/{id}` PATCH + `/cancel` gate on `('sample','requisition','edit')` → after Task 6, `business_head`+admin only.
- `requisitions/{id}/npd-review` gates on `('sample','npd',create)` → npd_team. ✓
- `npd-dev-job-cards/{id}/promote-approval` is `view`-gated but `act_promote_approval` enforces gate identity (INV_MGR=inventory_manager, REQUESTOR_BH=requestor user_id). ✓

- [ ] **Step 2: Document the GET over-exposure finding (no fix by default)**

Note in the spec/PR description: dev-JC and requisition GET routes require only `('sample',view)`, which `floor_manager`/`planner`/`viewer` also hold — they cannot see the NPD module UI but could read NPD data via direct API calls. Fixing cleanly needs a new `('sample','npd','view')` permission granted to the 4 roles + swapping the NPD GET routes to it. **Out of scope / optional follow-up** (the user chose "report without breaking"). Surface to the user as a decision.

- [ ] **Step 3: (no commit — verification only)**

---

## Self-Review (completed by author)

- **Spec coverage:** Module visibility (Tasks 2,3) ✓ · BH/NPD/IM matrix (already-enforced, re-verified in Task 7; create allowed for BH via existing canRequest) ✓ · outpass gate (Task 4) ✓ · multi-role (Tasks 1,5) ✓ · backend verify/harden (Tasks 6,7) ✓ · NPD "no edit/cancel" enforced backend (Task 6) ✓.
- **Placeholder scan:** none — all steps carry concrete code/commands.
- **Type consistency:** `roleNamesOf`, `canSeeNpdModule`, `canOutpass`, `canSeeNpd` defined in Task 1 and used consistently in Tasks 2–5. `allowedRoles` defined in Task 2 Step 1 and used in Step 3.
- **Out of scope (unchanged):** shared `/modules/sample/[id]` lockdown; standalone Sample module; "Outpass No" label; GET-exposure narrowing (reported only).
