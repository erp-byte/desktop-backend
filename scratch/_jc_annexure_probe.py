"""Verification probe for annexure PATCH + DELETE endpoints.

For each of the 5 annexure types (environment, metal-detection, weight-check,
loss-reconciliation, remarks), the probe:
  1. Auto-discovers an existing row on the parent JC (skips that annexure if none)
  2. PATCH-updates a single field, verifies the others are preserved
  3. Tries a cross-JC PATCH (URL says jc_b but row belongs to jc_a) -> expects 404
  4. Restores the original column value

DELETE is exercised on remarks only (cheapest to lose), guarded by a flag.
By default DELETE is SKIPPED to preserve your test data; pass --include-delete
to enable it (but only if you can afford to lose those rows).

Usage:
  $env:DB_URL    = 'postgresql://...'
  $env:TOKEN     = '<bearer>'
  python _jc_annexure_probe.py <jc_a_id> <jc_b_id> [--include-delete]

  jc_a_id  -- the job card whose annexure rows we'll edit (must have status in
              {locked, unlocked, assigned, material_received, in_progress})
  jc_b_id  -- a DIFFERENT job card, used to test cross-JC isolation. Status
              doesn't matter; we never modify jc_b's data, only attempt 404s.
"""
import asyncio
import os
import sys

import asyncpg
import httpx

DB_URL = os.environ["DB_URL"]
BASE = os.environ.get("BASE_URL", "http://localhost:8000")
TOKEN = os.environ["TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ─── Annexure descriptors ───────────────────────────────────────────────────
# Each entry says: (url-segment, table, pk-col, change-col, fixed-test-value, restore-from-dict-key)
ANNEXURES = [
    {
        "name":      "environment",
        "url":       "environment",
        "table":     "job_card_environment",
        "pk":        "env_id",
        "change":    "parameter_name",
        "test_val":  "PROBE_TEMP",
        "preserve":  ["value"],
    },
    {
        "name":      "metal_detection",
        "url":       "metal-detection",
        "table":     "job_card_metal_detection",
        "pk":        "detection_id",
        "change":    "failed_units",
        "test_val":  99,
        "preserve":  ["check_type", "fe_pass", "nfe_pass", "ss_pass", "remarks"],
    },
    {
        "name":      "weight_check",
        "url":       "weight-checks",
        "table":     "job_card_weight_check",
        "pk":        "check_id",
        "change":    "leak_test_pass",
        "test_val":  False,
        "preserve":  ["sample_number", "net_weight", "gross_weight"],
    },
    {
        "name":      "loss_reconciliation",
        "url":       "loss-reconciliation",
        "table":     "job_card_loss_reconciliation",
        "pk":        "recon_id",
        "change":    "remarks",
        "test_val":  "PROBE_REMARK",
        "preserve":  ["loss_category", "budgeted_loss_pct", "budgeted_loss_kg",
                      "actual_loss_kg", "variance_kg"],
    },
    {
        "name":      "remarks",
        "url":       "remarks",
        "table":     "job_card_remarks",
        "pk":        "remark_id",
        "change":    "content",
        "test_val":  "PROBE updated content",
        "preserve":  ["remark_type", "recorded_by"],
    },
]


async def _find_first_row(db, table, pk, jc_id):
    return await db.fetchrow(
        f"SELECT * FROM {table} WHERE job_card_id = $1 AND deleted_at IS NULL "
        f"ORDER BY {pk} LIMIT 1",
        jc_id,
    )


async def _snapshot(db, table, pk, pk_val):
    return dict(await db.fetchrow(
        f"SELECT * FROM {table} WHERE {pk} = $1", pk_val,
    ))


async def test_annexure(client, db, descr, jc_a, jc_b):
    name = descr["name"]
    print(f"\n{'='*70}\n  {name}\n{'='*70}")

    row = await _find_first_row(db, descr["table"], descr["pk"], jc_a)
    if row is None:
        print(f"  [skip] no {descr['table']} rows on jc_id={jc_a} -- nothing to test")
        return True
    pk_val = row[descr["pk"]]
    before = dict(row)
    print(f"  found {descr['pk']}={pk_val}, "
          f"{descr['change']}={before[descr['change']]!r}")

    # --- Test A: partial-update preserves the other columns ---
    body = {descr["change"]: descr["test_val"], "updated_by": "probe"}
    r = await client.patch(
        f"/api/v1/production/job-cards/{jc_a}/{descr['url']}/{pk_val}",
        json=body,
    )
    if r.status_code != 200:
        print(f"  [FAIL] PATCH expected 200, got {r.status_code}: {r.text}")
        return False
    after = await _snapshot(db, descr["table"], descr["pk"], pk_val)
    if after[descr["change"]] != descr["test_val"]:
        print(f"  [FAIL] {descr['change']} should be {descr['test_val']!r}, "
              f"got {after[descr['change']]!r}")
        return False
    for col in descr["preserve"]:
        if after[col] != before[col]:
            print(f"  [FAIL] {col} should be unchanged: "
                  f"{before[col]!r} -> {after[col]!r}")
            return False
    if after["updated_at"] is None:
        print(f"  [FAIL] updated_at not stamped")
        return False
    if after["updated_by"] != "probe":
        print(f"  [FAIL] updated_by should be 'probe', got {after['updated_by']!r}")
        return False
    print(f"  [ok] PATCH preserved {len(descr['preserve'])} unrelated columns; "
          f"updated_at + updated_by stamped")

    # --- Test B: cross-JC PATCH returns 404 (url says jc_b, row belongs to jc_a) ---
    r = await client.patch(
        f"/api/v1/production/job-cards/{jc_b}/{descr['url']}/{pk_val}",
        json={descr["change"]: "HACK_VALUE", "updated_by": "probe"},
    )
    if r.status_code != 404:
        print(f"  [FAIL] cross-JC PATCH expected 404, got {r.status_code}: {r.text}")
        return False
    # And the row must NOT have changed (still 'PROBE_TEMP' / etc.)
    after2 = await _snapshot(db, descr["table"], descr["pk"], pk_val)
    if after2[descr["change"]] != descr["test_val"]:
        print(f"  [FAIL] cross-JC PATCH leaked through -- value changed to "
              f"{after2[descr['change']]!r}")
        return False
    print(f"  [ok] cross-JC PATCH rejected with 404; row not modified")

    # --- Restore the original value ---
    r = await client.patch(
        f"/api/v1/production/job-cards/{jc_a}/{descr['url']}/{pk_val}",
        json={descr["change"]: before[descr["change"]], "updated_by": "probe"},
    )
    if r.status_code != 200:
        print(f"  [warn] restore failed: {r.status_code} {r.text}")
    else:
        print(f"  (restored {descr['change']} to {before[descr['change']]!r})")

    return True


async def test_remarks_delete(client, db, jc_a):
    """Optional DELETE test on remarks. Creates a throwaway remark, then deletes it."""
    print(f"\n{'='*70}\n  remarks DELETE (optional)\n{'='*70}")

    # Create a throwaway remark
    r = await client.post(
        f"/api/v1/production/job-cards/{jc_a}/remarks",
        json={"remark_type": "observation", "content": "PROBE throwaway",
              "recorded_by": "probe"},
    )
    if r.status_code != 200:
        print(f"  [FAIL] could not create throwaway remark: {r.status_code} {r.text}")
        return False
    new_id = r.json().get("remark_id") or r.json().get("row", {}).get("remark_id")
    if new_id is None:
        # Try DB lookup as fallback
        new_id = await db.fetchval(
            "SELECT remark_id FROM job_card_remarks WHERE job_card_id = $1 "
            "AND content = 'PROBE throwaway' ORDER BY remark_id DESC LIMIT 1",
            jc_a,
        )
    print(f"  created throwaway remark_id={new_id}")

    # Delete it
    r = await client.request(
        "DELETE", f"/api/v1/production/job-cards/{jc_a}/remarks/{new_id}",
        json={"deleted_by": "probe"},
    )
    if r.status_code != 200:
        print(f"  [FAIL] DELETE expected 200, got {r.status_code}: {r.text}")
        return False
    after = await _snapshot(db, "job_card_remarks", "remark_id", new_id)
    if after["deleted_at"] is None:
        print(f"  [FAIL] deleted_at not set")
        return False
    if after["deleted_by"] != "probe":
        print(f"  [FAIL] deleted_by should be 'probe', got {after['deleted_by']!r}")
        return False
    print(f"  [ok] DELETE marked deleted_at + deleted_by")

    # Second DELETE on same row -> 404
    r = await client.request(
        "DELETE", f"/api/v1/production/job-cards/{jc_a}/remarks/{new_id}",
        json={"deleted_by": "probe"},
    )
    if r.status_code != 404:
        print(f"  [FAIL] double-DELETE expected 404, got {r.status_code}")
        return False
    print(f"  [ok] double-DELETE rejected with 404")

    return True


async def main(jc_a: int, jc_b: int, include_delete: bool):
    db = await asyncpg.connect(DB_URL)
    client = httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=10.0)
    try:
        # Sanity: jc_a status must be editable
        jc_a_row = await db.fetchrow(
            "SELECT status FROM job_card WHERE job_card_id = $1 AND deleted_at IS NULL",
            jc_a,
        )
        if jc_a_row is None:
            print(f"  [FAIL] jc_id={jc_a} not found or already deleted"); return 1
        editable = {"locked", "unlocked", "assigned", "material_received", "in_progress"}
        if jc_a_row["status"] not in editable:
            print(f"  [FAIL] jc_id={jc_a} status='{jc_a_row['status']}' is not editable. "
                  f"Pick a JC with status in {sorted(editable)}.")
            return 1

        all_ok = True
        skipped = 0
        for descr in ANNEXURES:
            ok = await test_annexure(client, db, descr, jc_a, jc_b)
            if not ok:
                all_ok = False

        if include_delete:
            ok = await test_remarks_delete(client, db, jc_a)
            if not ok:
                all_ok = False

        print()
        print("=" * 70)
        if all_ok:
            print("  ALL ANNEXURE PROBES PASSED")
        else:
            print("  SOME PROBES FAILED -- see [FAIL] lines above")
        print("=" * 70)
        return 0 if all_ok else 1
    finally:
        await client.aclose()
        await db.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    rc = asyncio.run(main(int(args[0]), int(args[1]), "--include-delete" in flags))
    sys.exit(rc)
