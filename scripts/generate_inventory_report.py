"""
Inventory & Inward Report PDF
  - Section 1  : RM Stock per floor  vs  Production Plan commitment  =>  Net Available
  - Section 2  : PM Stock per floor  vs  Production Plan commitment  =>  Net Available
  - Section 3  : CFPL inward entries after 01-Apr-2026
  - Section 4  : CDPL inward entries after 01-Apr-2026
  - Section 5  : CFPL bulk entry stock (all entries)
  - Section 6  : CDPL bulk entry stock (all entries)

Usage:
    python scripts/generate_inventory_report.py
"""

import asyncio
import asyncpg
import os
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from fpdf import FPDF

DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://wmsadmin:Candorfoods@wms-postgres-db.cpis084golp7.ap-south-1.rds.amazonaws.com:5432/warehouse_db",
)
CUTOFF_DATE = date(2026, 4, 1)

# ── Helpers ───────────────────────────────────────────────────────────────────

def s(v, default="—"):
    if v is None:
        return default
    text = str(v)
    for old, new in {"\u2014":"-","\u2013":"-","\u2019":"'","\u201c":'"',"\u201d":'"',"\u2026":"..."}.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")

def fkg(v, zero="—"):
    if v is None or float(v) == 0:
        return zero
    return f"{float(v):,.1f}"

def fmt_date(v):
    if v is None:
        return "—"
    return v.strftime("%d-%b-%Y") if hasattr(v, "strftime") else str(v)

# ── Colours ───────────────────────────────────────────────────────────────────
NAV   = (58, 90, 140)
RM_H  = (20, 100, 60)    # dark green header for RM sections
PM_H  = (100, 50, 130)   # purple header for PM sections
LGRAY = (245, 246, 250)
DGRAY = (228, 234, 244)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED_BG= (255, 235, 235)
RED_T = (160, 30, 30)
AMB_T = (130, 80, 0)

FLOOR_LABEL = {
    "rm_store":        "RM Store",
    "pm_store":        "PM Store",
    "production_floor":"Production Floor",
    "fg_store":        "FG Store",
    "cold_store":      "Cold Store",
    "offgrade_store":  "Offgrade Store",
}

# ── PDF class ─────────────────────────────────────────────────────────────────

class ReportPDF(FPDF):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._hdr = ""

    def header(self):
        self.set_fill_color(*NAV)
        self.rect(0, 0, self.w, 11, "F")
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*WHITE)
        self.set_y(2.5)
        self.cell(0, 6, s(self._hdr), align="C")
        self.set_text_color(*BLACK)
        self.ln(9)

    def footer(self):
        self.set_y(-10)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 5,
            f"Page {self.page_no()}  |  Generated: {time.strftime('%d %b %Y %H:%M')}  |  Candor Foods",
            align="C")

    # ── section banner ──
    def banner(self, title, color):
        self._hdr = title
        self.set_fill_color(*color)
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 11)
        self.cell(0, 8, s(f"  {title}"), fill=True)
        self.ln(10)
        self.set_text_color(*BLACK)

    # ── table helpers ──
    def th(self, cols, color=DGRAY):
        self.set_fill_color(*color)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(*NAV)
        for lbl, w, al in cols:
            self.cell(w, 6, s(lbl), border=1, align=al, fill=True)
        self.ln()
        self.set_text_color(*BLACK)

    def tr(self, vals, cols, bg=WHITE, tc=BLACK):
        self.set_fill_color(*bg)
        self.set_text_color(*tc)
        self.set_font("Helvetica", "", 8)
        for val, (_, w, al) in zip(vals, cols):
            self.cell(w, 5.5, s(val), border=1, align=al, fill=True)
        self.ln()
        self.set_text_color(*BLACK)

    def tr_total(self, vals, cols, color=NAV):
        self.set_fill_color(*color)
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 8)
        for val, (_, w, al) in zip(vals, cols):
            self.cell(w, 6.5, s(val), border=1, align=al, fill=True)
        self.set_text_color(*BLACK)
        self.ln()

    def sub_banner(self, title, color):
        self.set_fill_color(*color)
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 9)
        self.cell(0, 6, s(f"   {title}"), fill=True)
        self.ln(7)
        self.set_text_color(*BLACK)

    def note(self, text):
        self.set_font("Helvetica", "I", 7.5)
        self.set_text_color(100, 100, 100)
        self.cell(0, 5, s(text))
        self.ln(6)
        self.set_text_color(*BLACK)

    def check_page(self, threshold=185):
        if self.get_y() > threshold:
            return True
        return False


# ── Queries ───────────────────────────────────────────────────────────────────

async def fetch_data():
    conn = await asyncpg.connect(DB_URL)

    # ── RM stock: latest stocktake entry per (warehouse, floor, item) ──────────
    rm_stock = await conn.fetch("""
        WITH latest_date AS (
            SELECT warehouse,
                   TRIM(floor_name)  AS floor_name,
                   item_name,
                   MAX(created_at::date) AS latest_date
            FROM stocktake_entries
            WHERE status = 'submitted'
              AND item_type = 'RM'
              AND floor_name IS NOT NULL
              AND TRIM(floor_name) <> ''
              AND TRIM(UPPER(floor_name)) NOT SIMILAR TO '%(TEST|TEESTTTT|TESTINGG)%'
              AND total_weight > 0
            GROUP BY warehouse, TRIM(floor_name), item_name
        )
        SELECT e.warehouse,
               TRIM(e.floor_name)  AS floor_name,
               e.item_name         AS sku_name,
               ROUND(SUM(e.total_weight)::numeric, 2) AS stock_kg
        FROM stocktake_entries e
        JOIN latest_date ld
          ON ld.warehouse       = e.warehouse
         AND TRIM(e.floor_name) = ld.floor_name
         AND e.item_name        = ld.item_name
         AND e.created_at::date = ld.latest_date
        WHERE e.status = 'submitted'
          AND e.item_type = 'RM'
        GROUP BY e.warehouse, TRIM(e.floor_name), e.item_name
        HAVING SUM(e.total_weight) > 0
        ORDER BY e.warehouse, TRIM(e.floor_name), stock_kg DESC
    """)

    # ── PM stock: latest stocktake entry per (warehouse, floor, item) ──────────
    pm_stock = await conn.fetch("""
        WITH latest_date AS (
            SELECT warehouse,
                   TRIM(floor_name)  AS floor_name,
                   item_name,
                   MAX(created_at::date) AS latest_date
            FROM stocktake_entries
            WHERE status = 'submitted'
              AND item_type = 'PM'
              AND floor_name IS NOT NULL
              AND TRIM(floor_name) <> ''
              AND TRIM(UPPER(floor_name)) NOT SIMILAR TO '%(TEST|TEESTTTT|TESTINGG)%'
              AND total_weight > 0
            GROUP BY warehouse, TRIM(floor_name), item_name
        )
        SELECT e.warehouse,
               TRIM(e.floor_name)  AS floor_name,
               e.item_name         AS sku_name,
               ROUND(SUM(e.total_weight)::numeric, 2) AS stock_kg
        FROM stocktake_entries e
        JOIN latest_date ld
          ON ld.warehouse       = e.warehouse
         AND TRIM(e.floor_name) = ld.floor_name
         AND e.item_name        = ld.item_name
         AND e.created_at::date = ld.latest_date
        WHERE e.status = 'submitted'
          AND e.item_type = 'PM'
        GROUP BY e.warehouse, TRIM(e.floor_name), e.item_name
        HAVING SUM(e.total_weight) > 0
        ORDER BY e.warehouse, TRIM(e.floor_name), stock_kg DESC
    """)

    # RM committed by approved production plans — across all entities
    rm_committed = await conn.fetch("""
        SELECT bl.material_sku_name AS sku_name,
               ROUND(SUM(
                   ppl.planned_qty_kg
                   * bl.quantity_per_unit
                   * (1 + COALESCE(bl.loss_pct, 0) / 100)
               )::numeric, 2) AS committed_kg
        FROM production_plan_line ppl
        JOIN production_plan pp ON pp.plan_id = ppl.plan_id
        JOIN bom_header       bh ON bh.bom_id = ppl.bom_id
        JOIN bom_line         bl ON bl.bom_id = bh.bom_id
        WHERE pp.status IN ('approved', 'in_progress')
          AND bl.item_type = 'rm'
          AND (ppl.status IS NULL OR ppl.status NOT IN ('completed', 'cancelled'))
        GROUP BY bl.material_sku_name
    """)

    # PM committed by approved production plans — across all entities
    pm_committed = await conn.fetch("""
        SELECT bl.material_sku_name AS sku_name,
               ROUND(SUM(
                   ppl.planned_qty_kg
                   * bl.quantity_per_unit
                   * (1 + COALESCE(bl.loss_pct, 0) / 100)
               )::numeric, 2) AS committed_kg
        FROM production_plan_line ppl
        JOIN production_plan pp ON pp.plan_id = ppl.plan_id
        JOIN bom_header       bh ON bh.bom_id = ppl.bom_id
        JOIN bom_line         bl ON bl.bom_id = bh.bom_id
        WHERE pp.status IN ('approved', 'in_progress')
          AND bl.item_type = 'pm'
          AND (ppl.status IS NULL OR ppl.status NOT IN ('completed', 'cancelled'))
        GROUP BY bl.material_sku_name
    """)

    # CFPL inward
    cfpl_inward = await conn.fetch("""
        SELECT t.transaction_no, t.entry_date,
               COALESCE(t.vendor_supplier_name, '(unknown)') AS vendor,
               t.warehouse,
               COALESCE(t.grn_number, '')  AS grn,
               t.status,
               a.item_description, a.material_type,
               a.lot_number, a.net_weight
        FROM cfpl_transactions_v2 t
        JOIN cfpl_articles_v2 a ON a.transaction_no = t.transaction_no
        WHERE t.entry_date >= $1
          AND (t.rtv     IS NULL OR t.rtv     = false)
          AND (t.service IS NULL OR t.service = false)
        ORDER BY t.entry_date, t.transaction_no
    """, CUTOFF_DATE)

    # CDPL inward
    cdpl_inward = await conn.fetch("""
        SELECT t.transaction_no, t.entry_date,
               COALESCE(t.vendor_supplier_name, '(unknown)') AS vendor,
               t.warehouse,
               COALESCE(t.grn_number, '')  AS grn,
               t.status,
               a.item_description, a.material_type,
               a.lot_number, a.net_weight
        FROM cdpl_transactions_v2 t
        JOIN cdpl_articles_v2 a ON a.transaction_no = t.transaction_no
        WHERE t.entry_date >= $1
          AND (t.rtv     IS NULL OR t.rtv     = false)
          AND (t.service IS NULL OR t.service = false)
        ORDER BY t.entry_date, t.transaction_no
    """, CUTOFF_DATE)

    # helper to query bulk entries for either entity prefix
    async def _bulk(prefix):
        return await conn.fetch(f"""
            SELECT
                t.transaction_no,
                t.entry_date,
                COALESCE(t.vendor_supplier_name, '(unknown)') AS vendor,
                t.warehouse,
                t.status                                        AS txn_status,
                a.item_description,
                a.material_type,
                a.lot_number,
                a.box_count                                     AS declared_boxes,
                COUNT(b.id)                                     AS scanned_boxes,
                COUNT(b.id) FILTER (WHERE b.status = 'available') AS available_boxes,
                ROUND(COALESCE(SUM(b.net_weight), 0)::numeric, 2)           AS total_net_kg,
                ROUND(COALESCE(SUM(b.net_weight) FILTER (WHERE b.status = 'available'), 0)::numeric, 2) AS available_kg
            FROM {prefix}_bulk_entry_transactions t
            JOIN {prefix}_bulk_entry_articles a ON a.transaction_no = t.transaction_no
            LEFT JOIN {prefix}_bulk_entry_boxes b ON b.transaction_no = t.transaction_no
            GROUP BY
                t.transaction_no, t.entry_date, t.vendor_supplier_name,
                t.warehouse, t.status,
                a.item_description, a.material_type, a.lot_number, a.box_count
            ORDER BY t.entry_date DESC, t.transaction_no
        """)

    cfpl_bulk = await _bulk("cfpl")
    cdpl_bulk = await _bulk("cdpl")

    await conn.close()
    return rm_stock, pm_stock, rm_committed, pm_committed, cfpl_inward, cdpl_inward, cfpl_bulk, cdpl_bulk


BULK_H = (140, 80, 20)   # orange-brown header for bulk sections


def build_bulk(pdf, rows, entity_label, section_num):
    title = f"Section {section_num} — {entity_label} Bulk Entry Stock  (all entries)"
    pdf.add_page(orientation="L", format="A4")
    pdf._hdr = title
    pdf.banner(title, BULK_H)

    if not rows:
        pdf.set_font("Helvetica", "I", 10)
        pdf.cell(0, 10, "No bulk entries found.")
        pdf.ln()
        return

    # ── stats bar ──────────────────────────────────────────────────────────────
    total_declared = sum(int(r["declared_boxes"] or 0) for r in rows)
    total_scanned  = sum(int(r["scanned_boxes"]  or 0) for r in rows)
    total_avail_b  = sum(int(r["available_boxes"] or 0) for r in rows)
    total_net      = sum(float(r["total_net_kg"]  or 0) for r in rows)
    total_avail_kg = sum(float(r["available_kg"]  or 0) for r in rows)
    pending        = sum(1 for r in rows if (r["txn_status"] or "").lower() == "pending")
    approved       = sum(1 for r in rows if (r["txn_status"] or "").lower() == "approved")

    stat_cols = [
        ("Transactions",      str(len({r['transaction_no'] for r in rows}))),
        ("Line Items",        str(len(rows))),
        ("Declared Boxes",    f"{total_declared:,}"),
        ("Scanned Boxes",     f"{total_scanned:,}"),
        ("Available Boxes",   f"{total_avail_b:,}"),
        ("Total Weight (kg)", fkg(total_net, "0")),
        ("Available (kg)",    fkg(total_avail_kg, "0")),
    ]
    w = 38
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*LGRAY)
    pdf.set_text_color(*BULK_H)
    for lbl, _ in stat_cols:
        pdf.cell(w, 6, f"  {lbl}", fill=True, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(250, 240, 230)
    for _, val in stat_cols:
        pdf.cell(w, 7, val, fill=True, border=1, align="R")
    pdf.set_text_color(*BLACK)
    pdf.ln(9)

    cols = [
        ("Date",            24, "C"),
        ("Transaction",     36, "L"),
        ("Vendor",          52, "L"),
        ("Item",            55, "L"),
        ("Type",            14, "C"),
        ("Lot No",          26, "L"),
        ("Decl. Boxes",     20, "R"),
        ("Scanned",         18, "R"),
        ("Avail. Boxes",    20, "R"),
        ("Total (kg)",      24, "R"),
        ("Available (kg)",  24, "R"),
        ("Warehouse",       18, "C"),
        ("Status",          20, "C"),
    ]
    pdf.th(cols, color=DGRAY)

    for i, r in enumerate(rows):
        status     = (r["txn_status"] or "").lower()
        net_kg     = float(r["total_net_kg"]  or 0)
        avail_kg   = float(r["available_kg"]  or 0)
        decl_b     = int(r["declared_boxes"]  or 0)
        scan_b     = int(r["scanned_boxes"]   or 0)
        avail_b    = int(r["available_boxes"] or 0)

        bg = LGRAY if i % 2 == 0 else WHITE
        tc = AMB_T if status == "pending" else BLACK

        pdf.tr(
            [fmt_date(r["entry_date"]),
             s(r["transaction_no"]),
             s(r["vendor"])[:38],
             s(r["item_description"])[:40],
             s(r["material_type"] or "—")[:6].upper(),
             s(r["lot_number"]),
             f"{decl_b:,}",
             f"{scan_b:,}",
             f"{avail_b:,}",
             fkg(net_kg),
             fkg(avail_kg),
             s(r["warehouse"]),
             status.upper()],
            cols, bg=bg, tc=tc
        )
        if pdf.check_page(185):
            pdf.add_page(orientation="L", format="A4")
            pdf._hdr = title + " (cont.)"
            pdf.th(cols, color=DGRAY)

    pdf.tr_total(
        ["", f"{len(rows)} lines", "", "", "", "TOTAL",
         f"{total_declared:,}", f"{total_scanned:,}", f"{total_avail_b:,}",
         fkg(total_net, "0"), fkg(total_avail_kg, "0"), "", ""],
        cols, color=BULK_H
    )

    if pending:
        pdf.ln(3)
        pdf.note(f"  {pending} transaction(s) still PENDING approval.  {approved} approved.")
    pdf.ln(3)


# ── PDF builders ──────────────────────────────────────────────────────────────

def build_cover(pdf):
    pdf.add_page(orientation="L", format="A4")
    pdf.set_fill_color(*NAV)
    pdf.rect(0, 65, 297, 75, "F")
    pdf.set_y(80)
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(*WHITE)
    pdf.cell(0, 15, "Inventory & Inward Report", align="C")
    pdf.ln(14)
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(200, 215, 240)
    pdf.cell(0, 8, "Candor Foods  |  CFPL & CDPL", align="C")
    pdf.set_y(158)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*NAV)
    pdf.cell(0, 6, "Contents", align="C")
    pdf.ln(9)
    contents = [
        ("Section 1", "RM  Stock — current floor stock vs production plan commitment vs net available"),
        ("Section 2", "PM  Stock — current floor stock vs production plan commitment vs net available"),
        ("Section 3", "CFPL Inward Entries  (1 Apr 2026 onwards)"),
        ("Section 4", "CDPL Inward Entries  (1 Apr 2026 onwards)"),
        ("Section 5", "CFPL Bulk Entry Stock  (declared boxes, scanned boxes, available qty)"),
        ("Section 6", "CDPL Bulk Entry Stock  (declared boxes, scanned boxes, available qty)"),
    ]
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(60, 60, 60)
    for sec, desc in contents:
        pdf.set_x(70)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(28, 6, s(sec))
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 6, s(desc))
        pdf.ln()
    pdf.set_y(197)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, f"Generated: {time.strftime('%d %b %Y  %H:%M')}", align="C")


def _build_material_section(pdf, stock_rows, committed_rows, item_type_label, section_num, hdr_color):
    """Builds RM or PM section grouped by LOCATION first, items within each location."""

    # committed lookup: sku_name -> committed_kg (across all entities)
    committed = {r["sku_name"]: float(r["committed_kg"]) for r in committed_rows}

    # group stock by (warehouse, physical floor_name)
    by_loc = defaultdict(list)
    for r in stock_rows:
        by_loc[(r["warehouse"], r["floor_name"])].append(r)

    cols = [
        (f"{item_type_label} — Item / SKU", 118, "L"),
        ("Stock (kg)",            36, "R"),
        ("Plan Committed (kg)",   44, "R"),
        ("Net Available (kg)",    40, "R"),
    ]

    title = f"Section {section_num} — {item_type_label} Stock by Location  (vs Production Plan)"
    pdf.add_page(orientation="L", format="A4")
    pdf._hdr = title
    pdf.banner(title, hdr_color)

    # ── overall summary bar ───────────────────────────────────────────────────
    total_stock = sum(float(r["stock_kg"]) for r in stock_rows)
    total_comm  = sum(committed.values())
    net_all     = total_stock - total_comm
    col_w = 79
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*LGRAY)
    pdf.set_text_color(*hdr_color)
    for lbl in ["Total Stock (kg)", "Plan Committed (kg)", "Net Available (kg)"]:
        pdf.cell(col_w, 6, f"  {lbl}", fill=True, border=1)
    pdf.ln()
    pdf.set_fill_color(*DGRAY)
    pdf.set_font("Helvetica", "B", 11)
    for val, is_net in [(total_stock, False), (total_comm, False), (net_all, True)]:
        clr = RED_T if (is_net and val < 0) else hdr_color
        pdf.set_text_color(*clr)
        pdf.cell(col_w, 8, fkg(val, "0") + " kg", fill=True, border=1, align="R")
    pdf.set_text_color(*BLACK)
    pdf.ln(10)
    pdf.note("Plan Committed = material required by all Approved production plans (BOM qty x loss%)")

    # ── one sub-section per location ──────────────────────────────────────────
    grand_stock = 0.0
    grand_comm  = 0.0

    for (warehouse, floor_name) in sorted(by_loc.keys()):
        loc_label = f"{warehouse} — {floor_name}"
        rows = by_loc[(warehouse, floor_name)]

        # aggregate sku totals at this location
        sku_totals = defaultdict(float)
        for r in rows:
            sku_totals[r["sku_name"]] += float(r["stock_kg"])

        loc_stock = sum(sku_totals.values())
        loc_comm  = sum(committed.get(sku, 0.0) for sku in sku_totals)
        loc_net   = loc_stock - loc_comm
        grand_stock += loc_stock
        grand_comm  += loc_comm

        # sub-banner with location totals inline
        pdf.set_fill_color(*hdr_color)
        pdf.set_text_color(*WHITE)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 6,
            s(f"   {loc_label}     |   Stock: {fkg(loc_stock, '0')} kg   "
              f"Committed: {fkg(loc_comm, '0')} kg   "
              f"Available: {fkg(loc_net, '0')} kg   ({len(sku_totals)} items)"),
            fill=True)
        pdf.ln(7)
        pdf.set_text_color(*BLACK)

        pdf.th(cols, color=DGRAY)

        for i, (sku, stock_kg) in enumerate(
                sorted(sku_totals.items(), key=lambda x: -x[1])):
            comm = committed.get(sku, 0.0)
            net  = stock_kg - comm
            bg   = RED_BG if net < 0 else (LGRAY if i % 2 == 0 else WHITE)
            tc   = RED_T  if net < 0 else BLACK
            pdf.tr(
                [sku[:90],
                 fkg(stock_kg),
                 fkg(comm) if comm else "—",
                 fkg(net)],
                cols, bg=bg, tc=tc
            )
            if pdf.check_page(185):
                pdf.add_page(orientation="L", format="A4")
                pdf._hdr = title + " (cont.)"
                pdf.banner(f"{loc_label}  (cont.)", hdr_color)
                pdf.th(cols, color=DGRAY)

        pdf.tr_total(
            [f"{loc_label.upper()}  TOTAL",
             fkg(loc_stock, "0"),
             fkg(loc_comm, "0"),
             fkg(loc_net, "0")],
            cols, color=hdr_color
        )
        pdf.ln(5)

    # ── grand total ───────────────────────────────────────────────────────────
    pdf.tr_total(
        ["GRAND TOTAL  (all locations)",
         fkg(grand_stock, "0"),
         fkg(grand_comm, "0"),
         fkg(grand_stock - grand_comm, "0")],
        cols, color=NAV
    )
    pdf.ln(4)


def build_inward(pdf, rows, entity_label, section_num):
    title = f"Section {section_num} — {entity_label} Inward Entries  (from 01 Apr 2026)"
    pdf.add_page(orientation="L", format="A4")
    pdf._hdr = title
    pdf.banner(title, NAV)

    if not rows:
        pdf.set_font("Helvetica", "I", 10)
        pdf.cell(0, 10, "No inward entries found for this period.")
        pdf.ln()
        return

    # summary stats
    approved   = sum(1 for r in rows if (r["status"] or "").lower() == "approved")
    pending    = sum(1 for r in rows if (r["status"] or "").lower() == "pending")
    total_wt   = sum(float(r["net_weight"]) for r in rows if r["net_weight"])
    total_tr   = len({r["transaction_no"] for r in rows})
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*LGRAY)
    pdf.set_text_color(*NAV)
    stat_cols = [
        ("Transactions", str(total_tr)),
        ("Line Items",   str(len(rows))),
        ("Approved",     str(approved)),
        ("Pending",      str(pending)),
        ("Total Qty Received", fkg(total_wt, "0") + " kg"),
    ]
    for lbl, val in stat_cols:
        pdf.cell(46, 6, f"  {lbl}", fill=True, border=1)
    pdf.ln()
    pdf.set_text_color(*NAV)
    pdf.set_font("Helvetica", "B", 10)
    for _, val in stat_cols:
        pdf.cell(46, 7, val, fill=True, border=1, align="R")
    pdf.set_text_color(*BLACK)
    pdf.ln(9)

    cols = [
        ("Date",         24, "C"),
        ("Transaction",  36, "L"),
        ("Vendor",       52, "L"),
        ("Item",         58, "L"),
        ("Type",         16, "C"),
        ("Lot No",       26, "L"),
        ("Qty (kg)",     26, "R"),
        ("Warehouse",    18, "C"),
        ("GRN",          28, "L"),
        ("Status",       20, "C"),
    ]
    pdf.th(cols)

    for i, r in enumerate(rows):
        qty    = float(r["net_weight"]) if r["net_weight"] else None
        status = (r["status"] or "").lower()
        bg     = LGRAY if i % 2 == 0 else WHITE
        tc     = AMB_T if status == "pending" else BLACK

        pdf.tr(
            [fmt_date(r["entry_date"]),
             r["transaction_no"] or "—",
             s(r["vendor"])[:38],
             s(r["item_description"])[:42],
             s(r["material_type"] or "—")[:8].upper(),
             s(r["lot_number"]),
             fkg(qty),
             s(r["warehouse"]),
             s(r["grn"])[:24],
             status.upper()],
            cols, bg=bg, tc=tc
        )
        if pdf.check_page(185):
            pdf.add_page(orientation="L", format="A4")
            pdf._hdr = title + " (cont.)"
            pdf.th(cols)

    pdf.tr_total(
        ["", f"{len(rows)} lines", "", "", "", "TOTAL",
         fkg(total_wt, "0") + " kg", "", "", ""],
        cols
    )
    pdf.ln(3)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("Connecting to database ...")
    rm_stock, pm_stock, rm_committed, pm_committed, \
        cfpl_inward, cdpl_inward, cfpl_bulk, cdpl_bulk = await fetch_data()
    print(f"  RM stock rows       : {len(rm_stock)}")
    print(f"  PM stock rows       : {len(pm_stock)}")
    print(f"  RM committed lines  : {len(rm_committed)}")
    print(f"  PM committed lines  : {len(pm_committed)}")
    print(f"  CFPL inward lines   : {len(cfpl_inward)}")
    print(f"  CDPL inward lines   : {len(cdpl_inward)}")
    print(f"  CFPL bulk lines     : {len(cfpl_bulk)}")
    print(f"  CDPL bulk lines     : {len(cdpl_bulk)}")

    pdf = ReportPDF()
    pdf.set_auto_page_break(auto=False)

    build_cover(pdf)
    _build_material_section(pdf, rm_stock, rm_committed, "Raw Material (RM)", "1", RM_H)
    _build_material_section(pdf, pm_stock, pm_committed, "Packaging Material (PM)", "2", PM_H)
    build_inward(pdf, cfpl_inward, "CFPL", "3")
    build_inward(pdf, cdpl_inward, "CDPL", "4")
    build_bulk(pdf, cfpl_bulk, "CFPL", "5")
    build_bulk(pdf, cdpl_bulk, "CDPL", "6")

    out_path = Path(__file__).parent.parent / "docs" / "reports" / "Inventory_Inward_Report_v4.pdf"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    print(f"\nSaved : {out_path}  ({pdf.page_no()} pages)")


if __name__ == "__main__":
    asyncio.run(main())
