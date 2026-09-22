"""Excel builder for the stock adjustment ledger (no DB access).

Pure openpyxl over rows the service already fetched, returning a BytesIO —
the same shape as customer_returns/services/export_xlsx.py.

The active filters are stamped into the sheet header. A spreadsheet outlives the
screen it was exported from, so a figure with no scope on it is unattributable
the moment it is emailed on; the Stock Take app does the same thing when it
writes "N floor(s) excluded" into its export.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .. import floors as _floors

# (key, header, width). Order is the sheet's column order.
COLUMNS: tuple[tuple[str, str, int], ...] = (
    # The 8-digit YYMMDD+NN reference, not the internal txn_id — this is the
    # number on screen, and a spreadsheet that used a different one could not be
    # cross-referenced against it.
    ("txn_code",         "Txn no.",        11),
    ("created_at",       "Date / time",    19),
    ("warehouse",        "Warehouse",      12),
    ("location",         "Floor",          20),
    ("item_name",        "Article",        44),
    ("material_type",    "Material type",  14),
    ("item_category",    "Category",       22),
    ("item_subcategory", "Sub category",   22),
    ("stock_type",       "Stock type",     18),
    ("operation",        "Operation",      13),
    ("units",            "Units",          10),
    ("qty_kg",           "Qty (kg)",       12),
    ("signed_kg",        "Signed (kg)",    12),
    ("reason",           "Reason",         40),
    ("created_by",       "Recorded by",    20),
    ("sku_id",           "SKU ID",         10),
    ("is_new_article",   "New article",    12),
    ("is_reversal",      "Reversal",       10),
    ("reverses_txn_code", "Reverses txn",  13),
)

_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_HEADER_FILL = PatternFill(start_color="29417A", end_color="29417A", fill_type="solid")
_TITLE_FONT = Font(bold=True, size=13)
_META_FONT = Font(size=9, color="666666")
_ADD_FILL = PatternFill(start_color="EAF6EC", end_color="EAF6EC", fill_type="solid")
_SUB_FILL = PatternFill(start_color="FDF0E6", end_color="FDF0E6", fill_type="solid")
_THIN = Side(style="thin")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _describe(filters: dict[str, Any]) -> str:
    if not filters:
        return "No filters — every recorded transaction."
    parts = []
    for key, label in (("warehouse", "Warehouse"), ("location", "Floor"), ("itemName", "Article"),
                       ("itemSearch", "Article contains"), ("stockType", "Stock type"),
                       ("operation", "Operation"), ("date", "Date"),
                       ("dateFrom", "From"), ("dateTo", "To")):
        if filters.get(key):
            parts.append(f"{label}: {filters[key]}")
    return "Filters — " + "; ".join(parts) if parts else "No filters — every recorded transaction."


def build_ledger_workbook(rows: list[dict[str, Any]], filters: dict[str, Any],
                          generated_by: str) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock Transactions"

    ws.cell(row=1, column=1, value="Stock adjustment ledger").font = _TITLE_FONT
    ws.cell(row=2, column=1, value=_describe(filters)).font = _META_FONT
    ws.cell(row=3, column=1,
            value=(f"{len(rows)} transaction(s) · exported "
                   f"{datetime.now().strftime('%Y-%m-%d %H:%M')} by {generated_by}")).font = _META_FONT

    head = 5
    for i, (_key, header, width) in enumerate(COLUMNS, start=1):
        c = ws.cell(row=head, column=i, value=header)
        c.font, c.fill, c.border = _HEADER_FONT, _HEADER_FILL, _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width

    for r, row in enumerate(rows, start=head + 1):
        add = row.get("operation") == "ADDITION"
        for i, (key, _header, _w) in enumerate(COLUMNS, start=1):
            if key == "signed_kg":
                # Direction is stored in `operation` and magnitudes are always
                # positive, so a signed column is derived here purely so the
                # spreadsheet can SUM() straight down to the net movement.
                v = (row.get("qty_kg") or 0) * (1 if add else -1)
            elif key == "created_at":
                v = str(row.get("created_at") or "").replace("T", " ")[:19]
            elif key in ("is_new_article", "is_reversal"):
                v = "Yes" if row.get(key) else ""
            else:
                v = row.get(key)
            c = ws.cell(row=r, column=i, value=v)
            c.border = _BORDER
            c.fill = _ADD_FILL if add else _SUB_FILL
            if key in ("units", "qty_kg", "signed_kg"):
                c.number_format = "#,##0.00" if key != "units" else "#,##0.000"
                c.alignment = Alignment(horizontal="right")

    ws.freeze_panes = ws.cell(row=head + 1, column=1)

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out


# ── Floor count export ─────────────────────────────────────────────────────
# Ported from backend_st/routes/exports.ts::exportStocktakeEntries. Columns 1-24
# are that sheet's columns in its own order, so the two files can be diffed
# row-for-row; "Batch ID" is appended as column 25 rather than inserted, so
# nothing existing shifts.
#
# (key, header, width). `key` names either a row field or a derived value
# handled in _entry_cell.
ENTRY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("id",               "Entry ID",                10),
    ("item_name",        "Item Name",               30),
    ("item_type",        "Item Type",               12),
    ("item_category",    "Category",                20),
    ("item_subcategory", "Subcategory",             20),
    ("floor_name",       "Floor Name",              15),
    ("warehouse",        "Warehouse",               15),
    ("total_quantity",   "Quantity (Units)",        15),
    ("unit_uom",         "Unit Weight (kg)",        15),
    ("total_weight",     "Total Weight (kg)",       15),
    ("entered_by",       "Entered By",              15),
    ("entered_by_email", "Email",                   25),
    ("authority",        "Authority",               15),
    ("stock_type",       "Stock Type",              18),
    ("created_at",       "Created At",              22),
    ("updated_at",       "Updated At",              22),
    ("verified",         "Verified",                12),
    ("verified_by",      "Verified By",             18),
    ("verified_at",      "Verified At",             22),
    ("remark",           "Remark",                  25),
    ("sig_entered",      "Entry By (Signature)",    35),
    ("created_at",       "Entry Submitted At",      22),
    ("verified_by",      "Verified By (Signature)", 25),
    ("verified_at",      "Verified At (Signature)", 22),
    ("entry_id",         "Batch ID",                12),
)

_NUM3 = {"total_quantity"}
_NUM2 = {"unit_uom", "total_weight"}

# Express's palette, kept so the two sheets read as the same document.
_FRESH_HEADER = "228B22"     # forest green
_REJECT_HEADER = "B22222"    # firebrick
_OTHER_HEADER = "29417A"     # house navy, for a stock type neither bucket claims
_UNVERIFIED_FILL = PatternFill(start_color="FAEEDA", end_color="FAEEDA", fill_type="solid")
_SUMMARY_HEAD_FILL = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
_TOTAL_FILL = PatternFill(start_color="EEEEEE", end_color="EEEEEE", fill_type="solid")
_FOOTER_FILL = PatternFill(start_color="F4F6FA", end_color="F4F6FA", fill_type="solid")
_FOOTER_FONT = Font(italic=True, size=9)
_BOLD = Font(bold=True)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _dt(value: Any) -> str:
    """Format as "DD Mon YYYY HH:MM", matching the floor app's formatDateDisplay.

    The value arrives already in IST — entries_export converts in SQL — so this
    only formats. Writing text rather than a real datetime is deliberate: it is
    what the floor app produces, and these columns are read, not calculated on.
    """
    if not isinstance(value, datetime):
        return ""
    return (f"{value.day:02d} {_MONTHS[value.month - 1]} {value.year} "
            f"{value.hour:02d}:{value.minute:02d}")


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _entry_cell(row: dict[str, Any], key: str) -> Any:
    if key == "sig_entered":
        name = row.get("entered_by") or ""
        email = row.get("entered_by_email") or ""
        return f"{name} — {email}" if email else name
    if key == "verified":
        return "Yes" if row.get("verified") is True else "No"
    if key in ("created_at", "updated_at", "verified_at"):
        return _dt(row.get(key))
    if key in _NUM3 or key in _NUM2:
        return _num(row.get(key))
    return row.get(key) or ""


def _stock_bucket(stock_type: Any) -> str:
    """Which sheet a row belongs on.

    Express drops anything outside its two hardcoded buckets from BOTH sheets
    while still counting it in the grand total, so the Summary would not add up.
    Only the two known values exist today (Fresh Stock 7296, Off Grade/Rejection
    1068), but the rule is made total here rather than left to hold by luck.
    """
    s = (stock_type or "Fresh Stock").strip()
    if s in ("Off Grade/Rejection", "Rejection"):
        return "Rejection"
    if s == "Fresh Stock":
        return "Fresh Stock"
    return s


# Excel forbids these in a sheet name and silently corrupts the file if present.
_SHEET_ILLEGAL = str.maketrans({c: "-" for c in '[]:*?/\\'})


def _sheet_name(bucket: str) -> str:
    return (bucket.translate(_SHEET_ILLEGAL).strip() or "Other")[:31]


def build_entries_workbook(rows: list[dict[str, Any]], filters: dict[str, Any],
                           draft_count: int, generated_by: str) -> BytesIO:
    """One sheet per stock type plus a Summary, as the floor app builds it."""
    verified_n = sum(1 for r in rows if r.get("verified") is True)
    subtitle = (f"Includes: {verified_n} verified + {len(rows) - verified_n} "
                "unverified entries")
    if draft_count:
        subtitle += f" | {draft_count} draft entries excluded — not yet submitted."

    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(_stock_bucket(row.get("stock_type")), []).append(row)
    # Fresh, then Rejection, then anything else — so today's data produces
    # exactly the floor app's sheet order.
    order = [b for b in ("Fresh Stock", "Rejection") if b in buckets]
    order += sorted(b for b in buckets if b not in ("Fresh Stock", "Rejection"))

    wb = Workbook()
    wb.remove(wb.active)
    for bucket in order:
        colour = {"Fresh Stock": _FRESH_HEADER,
                  "Rejection": _REJECT_HEADER}.get(bucket, _OTHER_HEADER)
        _entries_sheet(wb, _sheet_name(bucket), buckets[bucket], colour,
                       subtitle, filters, generated_by)
    # Always created, and created last, so an export that matched nothing is
    # still a valid workbook that says so rather than a download that fails.
    _summary_sheet(wb, order, buckets, len(rows), subtitle, filters, generated_by)

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def _entries_sheet(wb: Workbook, name: str, data: list[dict[str, Any]],
                   header_colour: str, subtitle: str, filters: dict[str, Any],
                   generated_by: str) -> None:
    ws = wb.create_sheet(name)
    ws.sheet_properties.defaultRowHeight = 18

    ws.cell(row=1, column=1, value=subtitle).font = _META_FONT
    # The floor app's sheet carries no record of what was filtered. A downloaded
    # figure with no scope on it is unattributable the moment it is emailed on,
    # so the same line the ledger export writes is added here.
    ws.cell(row=2, column=1, value=_describe(filters)).font = _META_FONT

    head = 4
    fill = PatternFill(start_color=header_colour, end_color=header_colour,
                       fill_type="solid")
    for i, (_key, header, width) in enumerate(ENTRY_COLUMNS, start=1):
        c = ws.cell(row=head, column=i, value=header)
        c.font, c.fill, c.border = _HEADER_FONT, fill, _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width

    for r, row in enumerate(data, start=head + 1):
        unverified = row.get("verified") is not True
        for i, (key, _header, _w) in enumerate(ENTRY_COLUMNS, start=1):
            c = ws.cell(row=r, column=i, value=_entry_cell(row, key))
            c.border = _BORDER
            c.alignment = Alignment(vertical="center")
            if key in _NUM3:
                c.number_format = "#,##0.000"
            elif key in _NUM2:
                c.number_format = "#,##0.00"
            # F1: an unverified row is tinted amber across its full width.
            if unverified:
                c.fill = _UNVERIFIED_FILL

    ws.freeze_panes = ws.cell(row=head + 1, column=1)

    # TOTAL row, in the floor app's own column positions (1, 7, 8, 10).
    total_row = head + len(data) + 2
    ws.cell(row=total_row, column=1, value="TOTAL:").font = _BOLD
    ws.cell(row=total_row, column=7, value=f"{len(data)} entries").font = _BOLD
    qty = ws.cell(row=total_row, column=8,
                  value=sum(_num(r.get("total_quantity")) for r in data))
    weight = ws.cell(row=total_row, column=10,
                     value=sum(_num(r.get("total_weight")) for r in data))
    for cell, fmt in ((qty, "#,##0.000"), (weight, "#,##0.00")):
        cell.font, cell.number_format = _BOLD, fmt
        cell.alignment = Alignment(horizontal="right")
    for col in (1, 7, 8, 10):
        ws.cell(row=total_row, column=col).fill = _TOTAL_FILL

    _footer(ws, total_row + 3, data, generated_by)


def _footer(ws: Any, start: int, data: list[dict[str, Any]], generated_by: str) -> int:
    """Signature block: who counted, who verified, who exported."""
    def line(row: int, text: str) -> int:
        c = ws.cell(row=row, column=1, value=text)
        c.font, c.fill = _FOOTER_FONT, _FOOTER_FILL
        return row + 1

    counters = sorted({(r.get("entered_by") or "").strip() for r in data} - {""})
    verifiers = sorted({(r.get("verified_by") or "").strip() for r in data} - {""})

    r = line(start, "Entry Team Signatures")
    for who in counters or ["(none)"]:
        r = line(r, f"  {who}")
    r = line(r, "")
    r = line(r, "Verified / Authorised By")
    for who in verifiers or ["(none)"]:
        r = line(r, f"  {who}")
    r = line(r, "")
    r = line(r, f"Exported by: {generated_by}")
    r = line(r, f"Export Date: {_dt(datetime.now())}")
    return line(r, "Candor ERP — Stock Take")


def _summary_sheet(wb: Workbook, order: list[str],
                   buckets: dict[str, list[dict[str, Any]]], total_rows: int,
                   subtitle: str, filters: dict[str, Any], generated_by: str) -> None:
    ws = wb.create_sheet("Summary")
    ws.sheet_properties.defaultRowHeight = 20
    ws.cell(row=1, column=1, value="Stock Type Summary").font = _TITLE_FONT
    ws.cell(row=2, column=1, value=_describe(filters)).font = _META_FONT

    head = 4
    for i, header in enumerate(("Stock Type", "Entries", "Total Quantity",
                                "Total Weight (kg)"), start=1):
        c = ws.cell(row=head, column=i, value=header)
        c.font, c.fill, c.border = _HEADER_FONT, _SUMMARY_HEAD_FILL, _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center")

    tint = {"Fresh Stock": "E8F5E9", "Rejection": "FFEBEE"}
    grand_qty = grand_weight = 0.0
    r = head
    # EVERY bucket present gets a row, so Entries down this column always sums to
    # the grand total. The floor app writes its two weights with toFixed(2),
    # which lands them in Excel as TEXT and cannot be summed; these are numbers.
    for bucket in order:
        r += 1
        data = buckets[bucket]
        qty = sum(_num(x.get("total_quantity")) for x in data)
        weight = sum(_num(x.get("total_weight")) for x in data)
        grand_qty, grand_weight = grand_qty + qty, grand_weight + weight
        for i, value in enumerate((bucket, len(data), qty, weight), start=1):
            c = ws.cell(row=r, column=i, value=value)
            c.border = _BORDER
            if i == 3:
                c.number_format = "#,##0.000"
            elif i == 4:
                c.number_format = "#,##0.00"
        shade = tint.get(bucket)
        if shade:
            ws.cell(row=r, column=1).fill = PatternFill(
                start_color=shade, end_color=shade, fill_type="solid")

    r += 1
    for i, value in enumerate(("GRAND TOTAL", total_rows, grand_qty, grand_weight),
                              start=1):
        c = ws.cell(row=r, column=i, value=value)
        c.font, c.fill, c.border = _BOLD, _TOTAL_FILL, _BORDER
        if i == 3:
            c.number_format = "#,##0.000"
        elif i == 4:
            c.number_format = "#,##0.00"

    for i, width in enumerate((26, 15, 18, 18), start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    if not order:
        # Nothing matched. Said plainly on the only sheet there is, rather than
        # leaving the recipient to guess whether the export failed.
        ws.cell(row=r + 2, column=1,
                value="No submitted count entries matched these filters.").font = _META_FONT
    _footer(ws, r + 4, [x for b in order for x in buckets[b]], generated_by)


# ── Current stock export ───────────────────────────────────────────────────
# The page's figures, per warehouse and floor: each article at its own latest
# count there, plus the adjustments posted there since. Two sheets, because the
# first question is "how much is where" and only then "which articles":
#   Summary         one line per warehouse + floor, a total per warehouse,
#                   then cold storage and grand totals
#   Stock by floor  one line per article at each floor, filterable
# Rows come from latest_stock_service.fetch_stock_by_place.

STOCK_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("warehouse",         "Warehouse",           16),
    ("floor",             "Floor",               20),
    ("item_name",         "Item",                42),
    ("item_type",         "Type",                 8),
    ("item_category",     "Group",               20),
    ("item_subcategory",  "Sub-group",           20),
    ("stock_type",        "Stock type",          18),
    ("total_quantity",    "Qty (units)",         12),
    ("counted_weight",    "Counted (kg)",        14),
    ("net_adjustment_kg", "Adjustments (kg)",    15),
    ("total_weight",      "Current stock (kg)",  17),
    ("last_counted_date", "Last counted",        13),
    ("days_since_count",  "Days since count",    11),
)
_KG = "#,##0.00"
_UNITS = "#,##0.###"
_DATE = "dd-mmm-yyyy"
_WH_TOTAL_FILL = PatternFill(start_color="E8EDF5", end_color="E8EDF5", fill_type="solid")
_COLD_FILL = PatternFill(start_color="E3F2FD", end_color="E3F2FD", fill_type="solid")
# The server runs on UTC (Lambda); the people reading this work to IST.
_IST = timezone(timedelta(hours=5, minutes=30))


def _describe_stock(filters: dict[str, Any]) -> str:
    parts = []
    whs = filters.get("warehouse")
    parts.append("Warehouse: " + (", ".join(_floors.warehouse_label(w) for w in whs)
                                  if whs else "all"))
    for key, label in (("floorName", "Floor"), ("itemType", "Type"), ("category", "Group"),
                       ("subcategory", "Sub-group"), ("stockType", "Stock type"),
                       ("search", "Search"), ("asOf", "As of")):
        v = filters.get(key)
        if v:
            parts.append(f"{label}: {', '.join(v) if isinstance(v, list) else v}")
    return "; ".join(parts)


def _warehouse_order(code: str) -> tuple[int, int, str]:
    """Factories A-Z first, then the cold stores in WAREHOUSE_LABELS order."""
    if code in _floors.COLD_WAREHOUSES:
        return (1, list(_floors.WAREHOUSE_LABELS).index(code), code)
    return (0, 0, code)


def _floor_order(code: str, floor_key: str) -> tuple[int, str]:
    """Declared floors in the order the building is walked, then the rest A-Z."""
    declared = [f.upper() for f in _floors.FLOORS_BY_WAREHOUSE.get(code, [])]
    return ((declared.index(floor_key), "") if floor_key in declared
            else (len(declared), floor_key))


def _floor_label(code: str, floor_key: str, spelled: str) -> str:
    """The declared spelling when there is one ("Store", not "STORE"), else as recorded."""
    for f in _floors.FLOORS_BY_WAREHOUSE.get(code, []):
        if f.upper() == floor_key:
            return f
    return (spelled or "").strip() or "(no floor)"


def build_stock_workbook(rows: list[dict[str, Any]], filters: dict[str, Any],
                         generated_by: str) -> BytesIO:
    # A place is warehouse + floor KEY, not the floor as spelled: one floor can
    # reach here as "STORE" from its counts and "Store" from its adjustments.
    places: dict[tuple[str, str], list[dict[str, Any]]] = {}
    labels: dict[tuple[str, str], str] = {}
    for r in rows:
        key = (r["warehouse"], r.get("floor_key") or (r.get("floor") or "").strip().upper())
        places.setdefault(key, []).append(r)
        labels.setdefault(key, _floor_label(key[0], key[1], r.get("floor") or ""))
    order = sorted(places, key=lambda p: (_warehouse_order(p[0]), _floor_order(*p)))

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(row=1, column=1, value="Current stock by warehouse and floor").font = _TITLE_FONT
    ws.cell(row=2, column=1,
            value="Each item at its own latest physical count, plus adjustments posted since.").font = _META_FONT
    ws.cell(row=3, column=1, value=_describe_stock(filters)).font = _META_FONT
    ws.cell(row=4, column=1,
            value=f"Exported {_dt(datetime.now(_IST))} IST by {generated_by}").font = _META_FONT

    head = 6
    headers = ("Warehouse", "Floor", "Items", "Counted (kg)", "Adjustments (kg)",
               "Current stock (kg)", "Off grade (kg)", "Last counted")
    for i, (header, width) in enumerate(zip(headers, (18, 22, 8, 15, 16, 18, 15, 13)), start=1):
        c = ws.cell(row=head, column=i, value=header)
        c.font, c.fill, c.border = _HEADER_FONT, _HEADER_FILL, _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width

    def totals(data: list[dict[str, Any]]) -> tuple:
        dates = [d["last_counted_date"] for d in data if d.get("last_counted_date")]
        # Items as the screen counts them: an item + stock type once, however
        # many floors it is on. Equal to the row count on a single floor.
        items = {(d.get("item_key") or (d.get("item_name") or "").strip().upper(),
                  d.get("stock_type")) for d in data}
        return (len(items),
                sum(_num(d["counted_weight"]) for d in data),
                sum(_num(d["net_adjustment_kg"]) for d in data),
                sum(_num(d["total_weight"]) for d in data),
                sum(_num(d["total_weight"]) for d in data
                    if d.get("stock_type") == "Off Grade/Rejection"),
                max(dates) if dates else None)

    r = head

    def line(first: str, second: str, data: list[dict[str, Any]],
             font: Any = None, fill: Any = None) -> None:
        nonlocal r
        r += 1
        for i, v in enumerate((first, second) + totals(data), start=1):
            c = ws.cell(row=r, column=i, value=v)
            c.border = _BORDER
            if font:
                c.font = font
            if fill:
                c.fill = fill
            if i in (4, 5, 6, 7):
                c.number_format = _KG
            elif i == 8:
                c.number_format = _DATE

    by_wh: dict[str, list[tuple[str, str]]] = {}
    for p in order:
        by_wh.setdefault(p[0], []).append(p)
    for code, wh_places in by_wh.items():
        label = _floors.warehouse_label(code) or "(no warehouse)"
        cold = code in _floors.COLD_WAREHOUSES
        for p in wh_places:
            line(label, labels[p], places[p], fill=_COLD_FILL if cold else None)
        line(f"{label} total", "", [x for p in wh_places for x in places[p]],
             font=_BOLD, fill=_WH_TOTAL_FILL)

    cold_rows = [x for x in rows if x["warehouse"] in _floors.COLD_WAREHOUSES]
    if cold_rows and len(cold_rows) < len(rows):
        # Only when both kinds are present; otherwise it would repeat the grand total.
        r += 1
        line("Factories and godowns", "",
             [x for x in rows if x["warehouse"] not in _floors.COLD_WAREHOUSES], font=_BOLD)
        line("Cold storage", "", cold_rows, font=_BOLD, fill=_COLD_FILL)
    line("GRAND TOTAL", "", rows, font=_BOLD, fill=_TOTAL_FILL)
    ws.freeze_panes = ws.cell(row=head + 1, column=1)
    if not rows:
        ws.cell(row=r + 2, column=1, value="No stock matched these filters.").font = _META_FONT

    # ── Sheet 2: every item, per floor ──
    ds = wb.create_sheet("Stock by floor")
    for i, (_key, header, width) in enumerate(STOCK_COLUMNS, start=1):
        c = ds.cell(row=1, column=i, value=header)
        c.font, c.fill, c.border = _HEADER_FONT, _HEADER_FILL, _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ds.column_dimensions[get_column_letter(i)].width = width
    n = 1
    for p in order:
        for item in places[p]:
            n += 1
            for i, (key, _header, _w) in enumerate(STOCK_COLUMNS, start=1):
                v = (_floors.warehouse_label(item["warehouse"]) if key == "warehouse"
                     else labels[p] if key == "floor" else item.get(key))
                c = ds.cell(row=n, column=i, value=v)
                if key == "total_quantity":
                    c.number_format = _UNITS
                elif key in ("counted_weight", "net_adjustment_kg", "total_weight"):
                    c.number_format = _KG
                elif key == "last_counted_date":
                    c.number_format = _DATE
            if item["warehouse"] in _floors.COLD_WAREHOUSES:
                ds.cell(row=n, column=1).fill = _COLD_FILL
    ds.freeze_panes = "C2"
    ds.auto_filter.ref = f"A1:{get_column_letter(len(STOCK_COLUMNS))}{max(n, 1)}"

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out
