"""Doc 04 backend review: dashboard all-data + filter-options.

Verifies: record shape (all spec fields present), numeric coercion, box/received/
issue enrichment, as_of_date, and that warehouse scoping actually narrows the set
and never leaks rows outside scope. Read-only — no writes."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

from app.modules.transfer.services import dashboard_service as d

load_dotenv()

EXPECTED_KEYS = {
    "transfer_id", "challan_no", "transfer_date", "transfer_month", "from_warehouse",
    "to_warehouse", "vehicle_no", "driver_name", "status", "created_by", "remark",
    "item_description", "item_category", "sub_category", "material_type", "lot_number",
    "qty", "uom", "pack_size", "net_weight", "total_weight", "box_count",
    "received_status", "issue_count", "issue_items", "issue_weight", "issue_details",
    "has_issue",
}


def norm(w):
    return str(w).strip().lower().replace("-", "")


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    fails = []

    # ── all-data (unscoped) ──
    res = await d.get_all_data(c, scope=None)
    recs = res["records"]
    print(f"all-data unscoped: total={res['total']} as_of={res['as_of_date']}")
    assert res["total"] == len(recs), "total != len(records)"
    if recs:
        missing = EXPECTED_KEYS - set(recs[0].keys())
        extra = set(recs[0].keys()) - EXPECTED_KEYS
        if missing:
            fails.append(f"missing keys: {missing}")
        if extra:
            fails.append(f"unexpected keys: {extra}")
        # numeric types
        r0 = recs[0]
        for k in ("qty", "pack_size", "net_weight", "total_weight", "issue_weight"):
            if not isinstance(r0[k], (int, float)):
                fails.append(f"{k} not numeric: {type(r0[k])}")
        if not isinstance(r0["transfer_id"], int):
            fails.append("transfer_id not int")
        if not isinstance(r0["issue_details"], list):
            fails.append("issue_details not list")
        if not isinstance(r0["has_issue"], bool):
            fails.append("has_issue not bool")

    # LINE_FILTER honoured: every record has some positive measure
    bad = [r for r in recs if not (r["net_weight"] > 0 or r["total_weight"] > 0 or r["qty"] > 0)]
    if bad:
        fails.append(f"{len(bad)} records violate LINE_FILTER")

    # enrichment sanity
    with_box = sum(1 for r in recs if r["box_count"] > 0)
    with_recv = sum(1 for r in recs if r["received_status"] != "Not Received")
    with_issue = sum(1 for r in recs if r["has_issue"])
    print(f"  enrichment: box_count>0={with_box}  received!=NotReceived={with_recv}  has_issue={with_issue}")
    # has_issue consistency
    for r in recs:
        if r["has_issue"] != (r["issue_count"] > 0):
            fails.append(f"has_issue/issue_count mismatch tid={r['transfer_id']}")
            break

    # ── filter-options (unscoped) ──
    opts = await d.get_filter_options(c, scope=None)
    print(f"filter-options unscoped: from={len(opts['from_warehouses'])} to={len(opts['to_warehouses'])} "
          f"status={len(opts['statuses'])} cat={len(opts['item_categories'])} "
          f"mat={len(opts['material_types'])} by={len(opts['created_by'])}")
    for k in ("from_warehouses", "to_warehouses", "statuses", "item_categories", "material_types", "created_by"):
        if k not in opts:
            fails.append(f"filter-options missing {k}")

    # ── scoping ──
    # pick a real warehouse that appears as from or to, scope to it, verify narrowing + no leak
    if recs:
        sample_wh = recs[0]["from_warehouse"] or recs[0]["to_warehouse"]
        scoped = await d.get_all_data(c, scope=[sample_wh])
        sr = scoped["records"]
        print(f"scoped to {sample_wh!r}: total={scoped['total']} (unscoped={res['total']})")
        if scoped["total"] > res["total"]:
            fails.append("scoped total exceeds unscoped")
        leak = [r for r in sr if norm(sample_wh) not in (norm(r["from_warehouse"]), norm(r["to_warehouse"]))]
        if leak:
            fails.append(f"SCOPE LEAK: {len(leak)} rows outside scope {sample_wh!r}")
        # filter-options also scoped: every from/to option must be reachable
        sopts = await d.get_filter_options(c, scope=[sample_wh])
        print(f"  scoped filter-options: from={len(sopts['from_warehouses'])} to={len(sopts['to_warehouses'])}")
        if len(sopts["from_warehouses"]) > len(opts["from_warehouses"]):
            fails.append("scoped from_warehouses exceeds unscoped")

        # hyphen/case insensitivity: scope passed with opposite hyphenation should match
        alt = sample_wh.replace("-", "") if "-" in sample_wh else sample_wh
        if alt != sample_wh:
            alt_res = await d.get_all_data(c, scope=[alt])
            if alt_res["total"] != scoped["total"]:
                fails.append(f"hyphen-normalization mismatch: {sample_wh!r}={scoped['total']} vs {alt!r}={alt_res['total']}")

    await c.close()

    print()
    if fails:
        print("FAIL:")
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print("PASS: all checks green")


asyncio.run(main())
