"""Pending Orders export: the "Pending orders - Floor wise plan" sheet, built
from the ERP instead of by hand.

Per SO line: Purchase Order Qty. (so_line.quantity) - Dispatch Qty. (the
invoiced quantity of that SO line, from sales_invoice_line, cancelled invoices
left out) = Pending Qty. Quantities are compared as counts, as the sheet does:
checked 19 Nov 2026, invoices count in the same unit as their SO line (the kg
figures differ more often, because some SO kg are still wrong).

Columns follow the sheet (38). Ones the ERP doesn't hold (PO Date, PO
Validity/Group, Sales Head, Location, Executed By, PO Closur Date1, the
planned-dispatch dates) are left blank. Packed to be / Floor / Person come
from the production plan linked to the SO line (its packing job card and the
planner), when there is one.
"""

from __future__ import annotations

import io
import re
from collections import defaultdict
from datetime import date
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

COLUMNS = [
    "Order Date", "PO Date", "Company", "PO Validity/Group", "Group", "PO No", "Sales Head", "SO No",
    "Location", "Customer Name", "Packed to be", "Executed By", "Floor", "Person", "Description", "Uom",
    "Total Kg", "UNITS/KGS", "Purchase Order Qty.", "Dispatch Qty.", "Pending Qty.", "Dispatch Date",
    "Invoice No.", "Days", "Basic Rate", "Basic Value", "Line Fill", "Pending Value", "In CR",
    "Dispatch Value", "Current Date", "Over Days", "Ageing Days", "PO Closur Date1",
    "Planned Dispatched Date", "Pending Planned Dispatched Date", "Remark", "Remarks 1",
]
_DATE_COLS = {"Order Date", "PO Date", "Dispatch Date", "Current Date", "PO Closur Date1",
              "Planned Dispatched Date", "Pending Planned Dispatched Date"}
# Money columns: dropped for roles that may not see cost
# (production.services.response_filters.sees_cost), in the sheet and the dashboard.
COST_COLUMNS = frozenset({"Basic Rate", "Basic Value", "Pending Value", "In CR", "Dispatch Value"})


def visible_columns(sees_cost: bool) -> list[str]:
    return COLUMNS if sees_cost else [c for c in COLUMNS if c not in COST_COLUMNS]
_SO_STYLE = re.compile(r"^[A-Za-z]{1,5}-SO/\d{2}-\d{2}/\d+$", re.I)

_LINES_SQL = """
SELECT h.so_number, h.so_date, h.company, h.customer_name,
       l.so_line_id, l.line_number, l.sku_name, l.sales_group, l.uom AS pack,
       l.quantity, l.rate_inr, l.amount_inr, f.kg AS ordered_kg
FROM so_line l
JOIN so_header h ON h.so_id = l.so_id
LEFT JOIN (SELECT so_line_id, sum(original_qty_kg) AS kg FROM so_fulfillment_v2 GROUP BY so_line_id) f
       ON f.so_line_id = l.so_line_id
ORDER BY h.so_date, h.so_number, l.line_number
"""

_DISPATCH_SQL = """
SELECT il.so_line_id, i.invoice_no, i.invoice_date, il.quantity
FROM sales_invoice_line il
JOIN sales_invoice i ON i.invoice_no = il.invoice_no
WHERE il.so_line_id IS NOT NULL AND NOT i.cancelled
ORDER BY i.invoice_date, i.invoice_no
"""

_PLAN_SQL = """
SELECT f.so_line_id, p.created_by, jc.factory, jc.floor, jc.step_number
FROM so_fulfillment_v2 f
JOIN production_plan_line_v2 pl ON f.so_fulfillment_id = ANY(pl.linked_so_fulfillment_ids)
JOIN production_plan_v2 p ON p.plan_id = pl.plan_id AND p.status <> 'cancelled'
LEFT JOIN job_card_v2 jc ON jc.plan_line_id = pl.plan_line_id
      AND jc.deleted_at IS NULL AND jc.status <> 'cancelled'
"""


def ageing(days: int | None) -> str | None:
    """The sheet's buckets: 1-5, 6-10, 11-15, 16-20, >20 (0 = ordered today)."""
    if days is None:
        return None
    if days <= 0:
        return "0"
    for top, label in ((5, "1-5"), (10, "6-10"), (15, "11-15"), (20, "16-20")):
        if days <= top:
            return label
    return ">20"


async def fetch_inputs(conn) -> tuple[list[dict], dict[int, list[dict]], dict[int, dict], bool]:
    """(SO lines, dispatches by so_line_id, plan by so_line_id, invoice tables exist)."""
    lines = [dict(r) for r in await conn.fetch(_LINES_SQL)]
    have_invoices = bool(await conn.fetchval(
        "SELECT to_regclass('public.sales_invoice') IS NOT NULL "
        "AND to_regclass('public.sales_invoice_line') IS NOT NULL"))
    dispatches: dict[int, list[dict]] = defaultdict(list)
    if have_invoices:
        for r in await conn.fetch(_DISPATCH_SQL):
            dispatches[r["so_line_id"]].append(dict(r))
    plans: dict[int, dict] = {}
    best_step: dict[int, int] = {}
    persons: dict[int, set] = defaultdict(set)
    for r in await conn.fetch(_PLAN_SQL):
        sid = r["so_line_id"]
        if r["created_by"]:
            persons[sid].add(r["created_by"])
        step = r["step_number"] or 0
        if r["factory"] and step >= best_step.get(sid, -1):   # the packing (last) card's place
            best_step[sid] = step
            plans[sid] = {"factory": r["factory"], "floor": r["floor"]}
        plans.setdefault(sid, {"factory": None, "floor": None})
    for sid, names in persons.items():
        plans.setdefault(sid, {"factory": None, "floor": None})["person"] = ", ".join(sorted(names))
    return lines, dispatches, plans, have_invoices


def pending_rows(lines: list[dict], dispatches: dict[int, list[dict]], plans: dict[int, dict],
                 today: date, *, include_dispatched: bool = False) -> list[dict[str, Any]]:
    """One row per SO line still pending (or every line with include_dispatched)."""
    out: list[dict[str, Any]] = []
    for ln in lines:
        ordered = float(ln["quantity"] or 0)
        rate = float(ln["rate_inr"]) if ln["rate_inr"] is not None else None
        sent = dispatches.get(ln["so_line_id"], [])
        dispatched = round(sum(float(d["quantity"] or 0) for d in sent), 3)
        pending = round(max(0.0, ordered - dispatched), 3)
        if pending <= 0 and not include_dispatched:
            continue
        kg_per_unit = (round(float(ln["ordered_kg"]) / ordered, 4)
                       if ln.get("ordered_kg") and ordered > 0 else None)
        last = max((d["invoice_date"] for d in sent), default=None)
        so_date = ln["so_date"]
        plan = plans.get(ln["so_line_id"], {})
        over = dispatched - ordered
        basic_value = (float(ln["amount_inr"]) if ln["amount_inr"] is not None
                       else (ordered * rate if rate is not None else None))
        pending_value = round(pending * rate, 2) if rate is not None else None
        out.append({
            "so_line_id": ln["so_line_id"],          # row key for the dashboard; not a sheet column
            "Order Date": so_date,
            "Company": ln["company"],
            "Group": ln["sales_group"],
            "PO No": None if _SO_STYLE.match(ln["so_number"] or "") else ln["so_number"],
            "SO No": ln["so_number"],
            "Customer Name": ln["customer_name"],
            "Packed to be": plan.get("factory"),
            "Floor": plan.get("floor"),
            "Person": plan.get("person"),
            "Description": ln["sku_name"],
            "Uom": kg_per_unit,
            "Total Kg": round(pending * kg_per_unit, 3) if kg_per_unit else None,
            "UNITS/KGS": ("Kgs" if kg_per_unit == 1 else "Units") if kg_per_unit else ln["pack"],
            "Purchase Order Qty.": ordered,
            "Dispatch Qty.": dispatched or None,
            "Pending Qty.": pending,
            "Dispatch Date": last,
            "Invoice No.": ", ".join(dict.fromkeys(d["invoice_no"] for d in sent)) or None,
            "Days": (last - so_date).days if last and so_date else None,
            "Basic Rate": rate,
            "Basic Value": round(basic_value, 2) if basic_value is not None else None,
            "Line Fill": round(dispatched / ordered, 4) if ordered else None,
            "Pending Value": pending_value,
            "In CR": round(pending_value / 1e7, 6) if pending_value is not None else None,
            "Dispatch Value": round(dispatched * rate, 2) if rate is not None and dispatched else None,
            "Current Date": today,
            "Over Days": (today - so_date).days if so_date else None,
            "Ageing Days": ageing((today - so_date).days if so_date else None),
            "Remark": f"Invoiced {over:g} more than ordered" if over > 1e-6 else None,
        })
    return out


async def invoice_status(conn) -> dict[str, Any]:
    """What the dispatch side rests on: saved invoices and how many reach an SO line."""
    if not await conn.fetchval("SELECT to_regclass('public.sales_invoice') IS NOT NULL "
                               "AND to_regclass('public.sales_invoice_line') IS NOT NULL"):
        return {"tables": False, "invoices": 0, "lines_on_so_line": 0, "first_date": None, "last_date": None}
    r = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE NOT cancelled) AS invoices,
                  min(invoice_date) FILTER (WHERE NOT cancelled) AS first_date,
                  max(invoice_date) FILTER (WHERE NOT cancelled) AS last_date,
                  (SELECT count(*) FROM sales_invoice_line WHERE so_line_id IS NOT NULL) AS lines_on_so_line
           FROM sales_invoice""")
    return {"tables": True, **dict(r)}


def pending_orders_xlsx(rows: list[dict[str, Any]], today: date, *, note: str | None = None,
                        columns: list[str] | None = None) -> bytes:
    """The sheet's layout: a blank first row, headers on row 2, data from row 3."""
    columns = columns or COLUMNS
    wb = Workbook()
    ws = wb.active
    ws.title = "Pending orders"
    ws.cell(1, 1, note or f"Pending orders as of {today:%d.%m.%Y} (from the ERP)").font = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="FFFF00")
    thin = Side(style="thin", color="999999")
    for c, name in enumerate(columns, start=1):
        cell = ws.cell(2, c, name)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.column_dimensions[get_column_letter(c)].width = max(10, min(40, len(name) + 4))
    for name, width in (("Customer Name", 34), ("Description", 44), ("Invoice No.", 30), ("Remark", 30)):
        ws.column_dimensions[get_column_letter(columns.index(name) + 1)].width = width
    for r, row in enumerate(rows, start=3):
        for c, name in enumerate(columns, start=1):
            v = row.get(name)
            if v is None:
                continue
            cell = ws.cell(r, c, v)
            if name in _DATE_COLS:
                cell.number_format = "DD-MM-YYYY"
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(len(columns))}{max(2, len(rows) + 2)}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
