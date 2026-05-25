"""Negative-stock and _UNMAPPED drill-down report.

For each (unit, group) with closing < 0 in the v5 calc, dump:
  • Opening / Inward / Inf-In / Xfr-In / Xfr-Out / CN / Outward / Adj-In / Adj-Out totals
  • TOP 10 contributing invoices / transactions for the columns driving the
    negative (Outward + Adj-Out + Xfr-Out)
  • What invoice numbers, dates, customers, and SKUs

Also dumps the full list of articles landing in `_UNMAPPED` so the user can
classify them in the all_sku master.

Run:
    cd d:/Consumption/New/Backend
    python _negatives_drilldown_probe.py > _negatives_drilldown.txt
"""
import asyncio, importlib.util, sys, csv
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

spec = importlib.util.spec_from_file_location("m", "_closing_stock_calc_v5_probe.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def fmt(v):
    if abs(v) < 0.5: return "0"
    return f"{v:,.0f}"


async def main():
    # ── Load same buckets as v5 ──
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
        inward_u, inward_unmapped = await m.load_inward_per_unit(c, sku_info)
        txfr_out_u, txfr_in_u = await m.load_transfers_per_unit(c, sku_info)
        keys = set()
        for s in sales + excel_cns + pdf_cns:
            if s.get("doc_no"): keys.add((str(s["doc_no"]).strip().upper(), m.norm(s.get("article"))))
        do_rows = await m.load_direct_out(c, keys)
        sales = sales + do_rows
    await pool.close()

    aliases = m.build_aliases(bom)
    outward_u, adj_in_u, adj_out_u, out_class, out_unmap, sales_details, _ic = m.classify_sales(
        sales, sku_info, bom, aliases
    )
    cn_outward_u, cn_adj_in, cn_adj_out, _, _, cn_details, _ic2 = m.classify_sales(
        excel_cns + pdf_cns, sku_info, bom, aliases
    )

    # Inf-In Savla/Rishi only
    all_g = set()
    for b in (opening_u, inward_u, txfr_in_u, txfr_out_u, cn_outward_u,
              outward_u, adj_in_u, adj_out_u):
        for u in b:
            for g in b[u]: all_g.add(g)
    inferred_in_u = {u: defaultdict(float) for u in m.UNITS}
    for u in ("SAVLA", "RISHI"):
        for g in all_g:
            op = opening_u.get(u, {}).get(g, 0); iw = inward_u.get(u, {}).get(g, 0)
            tin = txfr_in_u.get(u, {}).get(g, 0); tout = txfr_out_u.get(u, {}).get(g, 0)
            if tout > 0 and tout > (op + iw + tin):
                inferred_in_u[u][g] = tout - (op + iw + tin)

    # Closing
    closing = {}
    for u in m.UNITS:
        for g in all_g:
            cl = (opening_u.get(u,{}).get(g,0) + inward_u.get(u,{}).get(g,0)
                + inferred_in_u.get(u,{}).get(g,0)
                + txfr_in_u.get(u,{}).get(g,0) - txfr_out_u.get(u,{}).get(g,0)
                + cn_outward_u.get(u,{}).get(g,0) - cn_adj_in.get(u,{}).get(g,0)
                + cn_adj_out.get(u,{}).get(g,0) - outward_u.get(u,{}).get(g,0)
                + adj_in_u.get(u,{}).get(g,0) - adj_out_u.get(u,{}).get(g,0))
            if cl < -0.5:
                closing[(u, g)] = cl

    # Group sales by (unit, group, doc_no) for the negative cells
    sales_by_unit_group = defaultdict(lambda: defaultdict(float))
    for s in sales:
        if not s.get("article"): continue
        kg = abs(float(s.get("kg") or 0))
        if kg <= 0: continue
        unit = s.get("unit") or "OTHER"
        if unit not in m.UNITS: unit = "OTHER"
        info = sku_info.get(m.norm(s.get("article")))
        if info and info.get("type") == "pm": continue
        own_group = m.smart_group(s.get("article"), info)
        sales_by_unit_group[(unit, own_group)][
            (s.get("doc_no", "?"), str(s.get("date") or "?"), s.get("article",""))
        ] += kg

    # Also: top BOM-RM contributors per (unit, group)
    # For each FG sale we know what RM groups it deducted from. Build adj_out detail.
    adj_out_by_unit_group_rm = defaultdict(lambda: defaultdict(float))
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
            rm_info = sku_info.get(m.norm(rm_name))
            rm_grp = m.smart_group(rm_name, rm_info)
            adj_out_by_unit_group_rm[(unit, rm_grp)][
                (s.get("doc_no","?"), str(s.get("date") or "?"), art, rm_name)
            ] += rm_kg

    # Transfers details
    pool = await m.connect_db()
    async with pool.acquire() as c:
        txfr_rows = await c.fetch("""
            SELECT h.challan_no, h.stock_trf_date, h.from_site, h.to_site,
                   h.status, l.item_category, l.item_desc_raw,
                   COALESCE(l.net_weight, 0) AS kg
            FROM interunit_transfers_lines l
            JOIN interunit_transfers_header h ON l.header_id = h.id
            WHERE h.stock_trf_date >= '2026-04-01'::date
        """)
    await pool.close()
    txfr_out_by_unit_group = defaultdict(list)
    for r in txfr_rows:
        kg = float(r["kg"] or 0)
        if kg <= 0: continue
        from_u = m.site_to_unit(r["from_site"])
        to_u = m.site_to_unit(r["to_site"])
        if from_u == to_u: continue
        g = m.norm_group(r["item_category"]) if r["item_category"] else m.smart_group(r["item_desc_raw"], sku_info.get(m.norm(r["item_desc_raw"])))
        if from_u == "A-185" and g == "packaging":
            from_u = "W-202"
        txfr_out_by_unit_group[(from_u, g)].append({
            "challan": r["challan_no"], "date": r["stock_trf_date"],
            "from": r["from_site"], "to": r["to_site"], "status": r["status"],
            "item": r["item_desc_raw"], "kg": kg,
        })

    # ── Emit report ──
    print("=" * 110)
    print(f"  NEGATIVE-STOCK DRILL-DOWN (v5, Inf-In rolled back to Savla/Rishi only)")
    print(f"  Window: 2026-04-01 → 2026-05-18  ·  {len(closing)} negative (unit, group) cells")
    print("=" * 110)

    for (u, g), cl in sorted(closing.items(), key=lambda x: x[1]):
        op = opening_u.get(u,{}).get(g,0); iw = inward_u.get(u,{}).get(g,0)
        tin = txfr_in_u.get(u,{}).get(g,0); tout = txfr_out_u.get(u,{}).get(g,0)
        ow = outward_u.get(u,{}).get(g,0); ai = adj_in_u.get(u,{}).get(g,0)
        ao = adj_out_u.get(u,{}).get(g,0); cn = cn_outward_u.get(u,{}).get(g,0)
        print()
        print("─" * 110)
        print(f"  UNIT={u}   GROUP={g}   CLOSING = {fmt(cl)} kg")
        print(f"    Opening {fmt(op):>10}  + Inward {fmt(iw):>10}  + Xfr-In {fmt(tin):>10}  − Xfr-Out {fmt(tout):>10}")
        print(f"    + CN    {fmt(cn):>10}  − Outward {fmt(ow):>9}  + Adj-In {fmt(ai):>10}  − Adj-Out {fmt(ao):>10}")
        print("─" * 110)
        if ow > 0:
            top_ow = sorted(sales_by_unit_group[(u, g)].items(), key=lambda x: -x[1])[:10]
            if top_ow:
                print(f"  TOP {len(top_ow)} OUTWARD invoices (article-group = {g}):")
                for (doc, dt, art), kg in top_ow:
                    print(f"    {fmt(kg):>10} kg  {doc:<22} {dt:<11} {art[:55]}")
        if ao > 0:
            top_ao = sorted(adj_out_by_unit_group_rm[(u, g)].items(), key=lambda x: -x[1])[:10]
            if top_ao:
                print(f"  TOP {len(top_ao)} ADJ-OUT (BOM-RM deductions, FG sales pulling {g}):")
                for (doc, dt, fg, rm), kg in top_ao:
                    print(f"    {fmt(kg):>10} kg  {doc:<22} {dt:<11} FG: {fg[:35]}  → RM: {rm[:30]}")
        if tout > 0:
            top_tx = sorted(txfr_out_by_unit_group[(u, g)], key=lambda x: -x["kg"])[:10]
            if top_tx:
                print(f"  TOP {len(top_tx)} TRANSFERS OUT (group = {g}):")
                for t in top_tx:
                    print(f"    {fmt(t['kg']):>10} kg  {t['challan']:<22} {str(t['date']):<11} "
                          f"{t['from']:<14} → {t['to']:<14} {t['status']:<10} {t['item'] or ''}")

    # ── _UNMAPPED detail ──
    print()
    print("=" * 110)
    print(f"  _UNMAPPED articles (not classifiable into any group)")
    print("=" * 110)
    print("\nSales register / PDF invoices — articles falling to _UNMAPPED:")
    for art, kg in sorted(out_unmap, key=lambda x: -x[1]):
        print(f"  {fmt(kg):>10} kg  {art}")
    print("\nInward boxes — articles NOT in all_sku (after vendor-fallback):")
    for art, kg in sorted(inward_unmapped.items(), key=lambda x: -x[1])[:20]:
        print(f"  {fmt(kg):>10} kg  {art!r}")


if __name__ == "__main__":
    asyncio.run(main())
