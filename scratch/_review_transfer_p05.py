"""Doc 05 backend review: create_request (rollback-verified) + dropdowns.

create_request runs inside a transaction we roll back, so nothing persists.
We assert: header inserted with status Pending, lines inserted, net-weight
computation (FG vs non-FG vs frontend-provided), response envelope shape, and
that after rollback nothing leaked. Dropdowns are read-only sanity checks."""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

from app.modules.transfer import schemas
from app.modules.transfer.services import request_service as rs
from app.modules.transfer.services import dropdown_service as dd

load_dotenv()


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    fails = []

    # ── dropdowns ──
    sites = await dd.get_warehouse_sites(c, True)
    print(f"warehouse-sites: {len(sites)} -> {[s['site_code'] for s in sites][:6]}")
    if not sites:
        fails.append("no warehouse sites")
    if sites and not {"id", "site_code", "site_name", "is_active"} <= set(sites[0]):
        fails.append("warehouse site shape wrong")

    drop = await dd.categorial_dropdown(c, None, None, None, None, 500, 0)
    mts = drop["options"]["material_types"]
    print(f"material_types: {mts}")
    if not set(mts) <= {"RM", "PM", "FG"}:
        fails.append(f"unexpected material types {mts}")
    # cascade one level
    if mts:
        d2 = await dd.categorial_dropdown(c, mts[0], None, None, None, 500, 0)
        cats = d2["options"]["item_categories"]
        print(f"  {mts[0]} -> {len(cats)} categories e.g. {cats[:3]}")
        if cats:
            d3 = await dd.categorial_dropdown(c, mts[0], cats[0], None, None, 500, 0)
            subs = d3["options"]["sub_categories"]
            print(f"    {cats[0]} -> {len(subs)} sub-categories e.g. {subs[:3]}")
            if subs:
                d4 = await dd.categorial_dropdown(c, mts[0], cats[0], subs[0], None, 500, 0)
                descs = d4["options"]["item_descriptions"]
                uoms = d4["options"]["uom_values"]
                print(f"      {subs[0]} -> {len(descs)} descriptions; uom_values len={len(uoms)}")
                if len(descs) != len(uoms):
                    fails.append("descs/uom length mismatch")

    srch = await dd.categorial_search(c, "oil", 10, 0)
    print(f"categorial-search 'oil': {srch['meta']['total_items']} total, {len(srch['items'])} returned")
    if srch["items"]:
        it = srch["items"][0]
        if not {"id", "item_description", "material_type", "group", "sub_group", "uom"} <= set(it):
            fails.append("search item shape wrong")
        if "oil" not in it["item_description"].lower():
            fails.append("search not matching term")

    # ── create_request (rollback-verified) ──
    # pick two distinct real site codes
    codes = [s["site_code"] for s in sites]
    frm, to = codes[0], codes[1] if len(codes) > 1 else codes[0]

    pre_headers = await c.fetchval("SELECT COUNT(*) FROM interunit_transfer_requests")

    tr = c.transaction()
    await tr.start()
    try:
        body = schemas.RequestCreate(
            form_data=schemas.FormDataCreate(
                request_date="05-06-2026", from_warehouse=frm, to_warehouse=to,
                reason_description="review test transfer",
            ),
            article_data=[
                schemas.ArticleDataCreate(
                    material_type="RM", item_category="TESTCAT", sub_category="TESTSUB",
                    item_description="TEST ITEM A", quantity="3", uom="box",
                    pack_size="2", unit_pack_size="0", net_weight="0",
                ),
                schemas.ArticleDataCreate(
                    material_type="FG", item_category="TESTCAT", sub_category="TESTSUB",
                    item_description="TEST ITEM B", quantity="2", uom="carton",
                    pack_size="5", unit_pack_size="4", net_weight="0",
                ),
                schemas.ArticleDataCreate(
                    material_type="PM", item_category="TESTCAT", sub_category="TESTSUB",
                    item_description="TEST ITEM C", quantity="10", uom="bag",  # invalid uom -> NULL
                    pack_size="1", net_weight="99.5",  # frontend-provided net wins
                ),
            ],
        )
        result = await rs.create_request(c, body, "reviewer@candorfoods.in")

        # header assertions
        if result["status"] != "Pending":
            fails.append(f"status not Pending: {result['status']}")
        if result["from_warehouse"] != frm or result["to_warehouse"] != to:
            fails.append("from/to not persisted")
        if result["reason_description"] != "REVIEW TEST TRANSFER":  # uppercased by validator
            fails.append(f"reason not uppercased: {result['reason_description']}")
        if len(result["lines"]) != 3:
            fails.append(f"expected 3 lines, got {len(result['lines'])}")

        uoms = {ln["item_description"]: ln["uom"] for ln in result["lines"]}
        if uoms.get("TEST ITEM A") != "BOX" or uoms.get("TEST ITEM B") != "CARTON":
            fails.append(f"valid uoms not stored: {uoms}")
        if uoms.get("TEST ITEM C") not in ("", None):
            fails.append(f"invalid uom BAG not coerced to NULL: {uoms.get('TEST ITEM C')!r}")

        nw = {ln["item_description"]: float(ln["net_weight"]) for ln in result["lines"]}
        # RM non-FG: pack_size*qty = 2*3 = 6
        if nw.get("TEST ITEM A") != 6.0:
            fails.append(f"RM net_weight wrong: {nw.get('TEST ITEM A')} (exp 6)")
        # FG: unit_pack*pack_size*qty = 4*5*2 = 40
        if nw.get("TEST ITEM B") != 40.0:
            fails.append(f"FG net_weight wrong: {nw.get('TEST ITEM B')} (exp 40)")
        # frontend-provided net wins
        if nw.get("TEST ITEM C") != 99.5:
            fails.append(f"frontend net not preferred: {nw.get('TEST ITEM C')} (exp 99.5)")

        # in-transaction the header exists
        in_txn = await c.fetchval("SELECT COUNT(*) FROM interunit_transfer_requests WHERE request_no=$1", result["request_no"])
        if in_txn != 1:
            fails.append("header not visible in-txn")
        line_ct = await c.fetchval("SELECT COUNT(*) FROM interunit_transfer_request_lines WHERE request_id=$1", result["id"])
        if line_ct != 3:
            fails.append(f"lines not visible in-txn: {line_ct}")
        print(f"in-txn: header+{line_ct} lines created, net weights {nw}")
    finally:
        await tr.rollback()

    post_headers = await c.fetchval("SELECT COUNT(*) FROM interunit_transfer_requests")
    if post_headers != pre_headers:
        fails.append(f"LEAK: header count {pre_headers} -> {post_headers}")
    print(f"post-rollback header count restored: {pre_headers} == {post_headers}")

    await c.close()
    print()
    if fails:
        print("FAIL:")
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print("PASS: all checks green")


asyncio.run(main())
