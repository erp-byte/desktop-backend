"""Negative-stock tabular dump for manual review.

Outputs two CSV files:
  _negatives_summary.csv   — one row per negative (unit, group) with all column totals
  _negatives_invoices.csv  — one row per top contributing record (outward/adj-out/xfr-out)
                              for each negative cell
"""
import asyncio, importlib.util, sys, csv
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

spec = importlib.util.spec_from_file_location("m", "_closing_stock_calc_v5_probe.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

SUMMARY_CSV = Path("_negatives_summary.csv")
DETAIL_CSV = Path("_negatives_invoices.csv")


async def main():
    opening_u = m.load_opening_per_unit()
    sales, excel_cns = m.load_excel_sales()
    seen = set()
    for s in sales + excel_cns:
        if s.get("doc_no"): seen.add(str(s["doc_no"]).strip().upper())
    pdf_inv, pdf_cns = m.load_invoice_extracts(seen)
    sales = sales + pdf_inv

    pool = await m.connect_db()
    async with pool.acquire() as c:
        sku_info = await m.load_sku_info(c)
        bom = await m.load_bom_master(c)
        inward_u, _ = await m.load_inward_per_unit(c, sku_info)
        txfr_out_u, txfr_in_u = await m.load_transfers_per_unit(c, sku_info)
        keys = set()
        for s in sales + excel_cns + pdf_cns:
            if s.get("doc_no"): keys.add((str(s["doc_no"]).strip().upper(), m.norm(s.get("article"))))
        do_rows = await m.load_direct_out(c, keys)
        sales = sales + do_rows
        txfr_rows = await c.fetch("""
            SELECT h.challan_no, h.stock_trf_date, h.from_site, h.to_site, h.status,
                   l.item_category, l.item_desc_raw, COALESCE(l.net_weight,0) AS kg
            FROM interunit_transfers_lines l
            JOIN interunit_transfers_header h ON l.header_id = h.id
            WHERE h.stock_trf_date >= '2026-04-01'::date
        """)
    await pool.close()

    aliases = m.build_aliases(bom)
    outward_u, adj_in_u, adj_out_u, *_ = m.classify_sales(sales, sku_info, bom, aliases)
    cn_outward_u, cn_adj_in, cn_adj_out, *_ = m.classify_sales(excel_cns + pdf_cns, sku_info, bom, aliases)

    # Savla/Rishi Inf-In
    all_g = set()
    for b in (opening_u, inward_u, txfr_in_u, txfr_out_u, cn_outward_u,
              outward_u, adj_in_u, adj_out_u):
        for u in b:
            for g in b[u]: all_g.add(g)
    inferred_in_u = {u: defaultdict(float) for u in m.UNITS}
    for u in ("SAVLA", "RISHI"):
        for g in all_g:
            op = opening_u.get(u,{}).get(g,0); iw = inward_u.get(u,{}).get(g,0)
            tin = txfr_in_u.get(u,{}).get(g,0); tout = txfr_out_u.get(u,{}).get(g,0)
            if tout > 0 and tout > (op + iw + tin):
                inferred_in_u[u][g] = tout - (op + iw + tin)

    # Identify negatives
    negatives = []
    for u in m.UNITS:
        for g in all_g:
            cl = (opening_u.get(u,{}).get(g,0) + inward_u.get(u,{}).get(g,0)
                + inferred_in_u.get(u,{}).get(g,0)
                + txfr_in_u.get(u,{}).get(g,0) - txfr_out_u.get(u,{}).get(g,0)
                + cn_outward_u.get(u,{}).get(g,0) - cn_adj_in.get(u,{}).get(g,0)
                + cn_adj_out.get(u,{}).get(g,0) - outward_u.get(u,{}).get(g,0)
                + adj_in_u.get(u,{}).get(g,0) - adj_out_u.get(u,{}).get(g,0))
            if cl < -0.5:
                negatives.append({
                    "unit": u, "group": g, "closing_kg": round(cl, 1),
                    "opening": round(opening_u.get(u,{}).get(g,0), 1),
                    "inward": round(inward_u.get(u,{}).get(g,0), 1),
                    "inf_in": round(inferred_in_u.get(u,{}).get(g,0), 1),
                    "xfr_in": round(txfr_in_u.get(u,{}).get(g,0), 1),
                    "xfr_out": round(txfr_out_u.get(u,{}).get(g,0), 1),
                    "cn_in": round(cn_outward_u.get(u,{}).get(g,0), 1),
                    "outward": round(outward_u.get(u,{}).get(g,0), 1),
                    "adj_in": round(adj_in_u.get(u,{}).get(g,0), 1),
                    "adj_out": round(adj_out_u.get(u,{}).get(g,0), 1),
                })

    # Build per-cell details for outward / adj_out / xfr_out
    sales_by_cell = defaultdict(lambda: defaultdict(lambda: {"kg":0,"customer":"","date":""}))
    for s in sales:
        if not s.get("article"): continue
        kg = abs(float(s.get("kg") or 0))
        if kg <= 0: continue
        unit = s.get("unit") or "OTHER"
        if unit not in m.UNITS: unit = "OTHER"
        info = sku_info.get(m.norm(s.get("article")))
        if info and info.get("type") == "pm": continue
        og = m.smart_group(s.get("article"), info)
        k = (s.get("doc_no","?"), s.get("article",""))
        entry = sales_by_cell[(unit, og)][k]
        entry["kg"] += kg
        entry["date"] = str(s.get("date") or "")
        entry["customer"] = (s.get("source","") or "")[:30]

    adj_out_by_cell = defaultdict(lambda: defaultdict(float))
    for s in sales:
        art = s.get("article")
        if not art: continue
        kg = abs(float(s.get("kg") or 0))
        if kg <= 0: continue
        unit = s.get("unit") or "OTHER"
        if unit not in m.UNITS: unit = "OTHER"
        info = sku_info.get(m.norm(art))
        if not (info and info.get("type") == "fg"): continue
        pcs = s.get("qty_pcs")
        try: pcs = float(pcs) if pcs else None
        except: pcs = None
        if not pcs: continue
        own_group = m.smart_group(art, info)
        b = m.expand_bom_recursive(m.norm(art), bom, aliases, sku_info, fg_group=own_group)
        if not b: continue
        for rm_name, q_per, it in b:
            if it == "pm": continue
            rm_kg = q_per * pcs
            rm_grp = m.smart_group(rm_name, sku_info.get(m.norm(rm_name)))
            adj_out_by_cell[(unit, rm_grp)][
                (s.get("doc_no","?"), str(s.get("date") or ""), art, rm_name)
            ] += rm_kg

    xfr_out_by_cell = defaultdict(list)
    for r in txfr_rows:
        kg = float(r["kg"] or 0)
        if kg <= 0: continue
        from_u = m.site_to_unit(r["from_site"]); to_u = m.site_to_unit(r["to_site"])
        if from_u == to_u: continue
        g = m.norm_group(r["item_category"]) if r["item_category"] else m.smart_group(r["item_desc_raw"], sku_info.get(m.norm(r["item_desc_raw"])))
        if from_u == "A-185" and g == "packaging": from_u = "W-202"
        xfr_out_by_cell[(from_u, g)].append({
            "challan": r["challan_no"], "date": str(r["stock_trf_date"]),
            "from": r["from_site"], "to": r["to_site"], "status": r["status"],
            "item": r["item_desc_raw"] or "", "kg": kg,
        })

    # Write summary CSV
    with open(SUMMARY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "unit","group","closing_kg","opening","inward","inf_in","xfr_in","xfr_out","cn_in","outward","adj_in","adj_out"
        ])
        w.writeheader()
        for r in sorted(negatives, key=lambda x: x["closing_kg"]):
            w.writerow(r)
    print(f"Wrote {SUMMARY_CSV} ({len(negatives)} negative cells)")

    # Write detail CSV
    with open(DETAIL_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["unit","group","closing_kg","source_column","doc_no","date","article_or_item","rm_or_route","kg","extra"])
        for n in sorted(negatives, key=lambda x: x["closing_kg"]):
            u, g, cl = n["unit"], n["group"], n["closing_kg"]
            # OUTWARD top 15
            top_ow = sorted(sales_by_cell[(u, g)].items(), key=lambda x: -x[1]["kg"])[:15]
            for (doc, art), v in top_ow:
                w.writerow([u, g, cl, "OUTWARD", doc, v["date"], art, "", round(v["kg"],1), v["customer"]])
            # ADJ-OUT top 15
            top_ao = sorted(adj_out_by_cell[(u, g)].items(), key=lambda x: -x[1])[:15]
            for (doc, dt, fg, rm), kg in top_ao:
                w.writerow([u, g, cl, "ADJ-OUT (BOM)", doc, dt, fg, rm, round(kg,1), ""])
            # XFR-OUT top 15
            top_tx = sorted(xfr_out_by_cell[(u, g)], key=lambda x: -x["kg"])[:15]
            for t in top_tx:
                w.writerow([u, g, cl, "XFR-OUT", t["challan"], t["date"], t["item"], f'{t["from"]}→{t["to"]} [{t["status"]}]', round(t["kg"],1), ""])
    print(f"Wrote {DETAIL_CSV}")

    # Also print summary table
    print()
    print(f"{'UNIT':<8} {'GROUP':<22} {'CLOSING':>10} {'Open':>9} {'Inward':>9} {'XfrIn':>8} {'XfrOut':>9} {'CN':>7} {'Out':>10} {'AdjIn':>9} {'AdjOut':>9}")
    print("-" * 130)
    for n in sorted(negatives, key=lambda x: x["closing_kg"]):
        print(f"{n['unit']:<8} {n['group']:<22} {n['closing_kg']:>+10,.0f} {n['opening']:>9,.0f} {n['inward']:>9,.0f} {n['xfr_in']:>8,.0f} {n['xfr_out']:>9,.0f} {n['cn_in']:>7,.0f} {n['outward']:>10,.0f} {n['adj_in']:>9,.0f} {n['adj_out']:>9,.0f}")


if __name__ == "__main__":
    asyncio.run(main())
