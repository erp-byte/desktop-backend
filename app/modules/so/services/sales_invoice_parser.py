"""Parser for the Tally "Sales Register" export: the invoices actually raised
(HO Sales, HO SERVICE GST, ...). It is the second upload on SO Creation; the
first one takes the Sales Order Book (so_book_parser).

Layout (export of 19 Nov 2026):
  * a preamble: company name and address lines, the title "Sales Register",
    then the period ("1-Nov-26 to 30-Nov-26");
  * one header row: Date | Particulars | Buyer | Buyer Address | Consignee |
    Consignee Address | Voucher Type | Voucher No. | Voucher Ref. No. |
    Narration | Quantity | Alt. Units | Rate | Value | Gross Total, then one
    column per ledger (SALES GST LOCAL, IGST (OUTPUT), CGST Output, Round Off,
    Freight & Transport Charges - Income, ...);
  * one row per voucher, with a Date and a Voucher No. (Particulars = the
    party), followed by its item rows, which have no Date (Particulars = the
    article, then Quantity / Alt. Units (kg) / Rate / Value);
  * "(cancelled )" as the party marks a cancelled voucher (no items);
  * a last "Grand Total" row.

Columns are found by their header text, so reordered columns are fine. Every
header the parser doesn't know is kept as a ledger amount on the voucher.
This module only reads the file; sales_invoices.process_sales_invoices adds
the article match, kg and the SO check.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Any

import openpyxl

from app.core.sites import canonical_site, warehouse_sites

# Header text (whitespace collapsed, lower case) -> field name.
_FIELDS: dict[str, str] = {
    "date": "date",
    "particulars": "particulars",
    "buyer": "buyer",
    "buyer address": "buyer_address",
    "consignee": "consignee",
    "consignee address": "consignee_address",
    "voucher type": "voucher_type",
    "voucher no.": "voucher_no",
    "voucher no": "voucher_no",
    "voucher ref. no.": "reference",
    "voucher ref no": "reference",
    "narration": "narration",
    "quantity": "quantity",
    "alt. units": "alt_units",
    "alt units": "alt_units",
    "rate": "rate",
    "value": "value",
    "gross total": "gross_total",
}
_REQUIRED = ("date", "particulars", "voucher_type", "voucher_no")
_HEADER_SCAN_ROWS = 40

_PERIOD = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{2,4}\s+to\s+\d{1,2}-[A-Za-z]{3}-\d{2,4}", re.I)
_CANCELLED = re.compile(r"\(\s*cancell?ed\s*\)", re.I)
_SO_NUMBER = re.compile(r"^[A-Za-z]{1,5}-SO/\d{2}-\d{2}/\d+$", re.I)
_DISPATCH_FROM = re.compile(r"dispatch(?:ed)?\s+from\s+([^.,;]+)", re.I)
_SITE_IN_PARENS = re.compile(r"\(\s*([A-Za-z]{1,2}\s*-?\s*\d{2,4})\s*\)")
_SITE_ALONE = re.compile(r"^\s*([A-Za-z]{1,2}\s*-?\s*\d{2,4})\s*\.?\s*$")
_CARTONS = re.compile(r"total\s+cart\w*\s*:?\s*(\d+)", re.I)
_REPLACES = re.compile(r"against\s+old\s+invoice\s+no\.?\s*:?\s*([A-Za-z0-9/\-]+?)\.?(?:\s|$)", re.I)
_NUMBER = re.compile(r"^(\()?\s*(-?\d+(?:\.\d+)?)")


def _header_key(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def _num(v: Any) -> float | None:
    """A cell as a number: 160, 160.5, "1,234.50", "160 Pcs", "(12.00)" -> -12."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 3)
    s = str(v).strip().replace(",", "")
    m = _NUMBER.match(s)
    if not m:
        return None
    x = float(m.group(2))
    return round(-x if m.group(1) else x, 3)


def _date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _text(v)
    if not s:
        return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _site_name(raw: str) -> str:
    """'202' -> 'W202', '185' -> 'A185', 'W-202' -> 'W202'; unknown text as written."""
    raw = raw.strip()
    if raw.isdigit():
        hits = [s for s in warehouse_sites() if re.sub(r"\D", "", s) == raw]
        return hits[0] if len(hits) == 1 else raw
    return canonical_site(raw)


def dispatch_site(narration: str | None) -> str | None:
    """Where the goods left from, read off the narration: "Dispatch From 202.",
    "Total Cartons : 50 ( W202 )", a bare "A185", "Material dispatch from savla cold."."""
    text = narration or ""
    for rx in (_DISPATCH_FROM, _SITE_IN_PARENS, _SITE_ALONE):
        m = rx.search(text)
        if m:
            return _site_name(m.group(1))
    return None


def cartons(narration: str | None) -> int | None:
    m = _CARTONS.search(narration or "")
    return int(m.group(1)) if m else None


def so_number_of(reference: str | None) -> str | None:
    """The reference upper-cased when it is written like one of our SO numbers
    (CF-SO/26-27/1404). Only a hint: many SOs are numbered with the customer's
    own PO number (8700942817, PO/00852), so the SO link itself is made by
    looking the reference up in so_header (sales_invoices)."""
    ref = (reference or "").strip()
    return ref.upper() if _SO_NUMBER.match(ref) else None


def _find_header(ws) -> tuple[int, dict[str, int], dict[int, str], list[tuple]]:
    """(header row index, field -> column, ledger column -> header, all rows)."""
    rows = list(ws.iter_rows(values_only=True))
    for i, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        fields: dict[str, int] = {}
        ledgers: dict[int, str] = {}
        for c, cell in enumerate(row):
            key = _header_key(cell)
            if not key:
                continue
            field = _FIELDS.get(key)
            if field and field not in fields:
                fields[field] = c
            elif not field:
                ledgers[c] = re.sub(r"\s+", " ", str(cell)).strip()
        if all(f in fields for f in _REQUIRED):
            return i, fields, ledgers, rows
    return -1, {}, {}, rows


def parse_sales_invoices(file_bytes: bytes) -> dict[str, Any]:
    """Read a Sales Register export. Raises ValueError when the file isn't one."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:  # not an xlsx at all
        raise ValueError(f"Could not open the file as an Excel workbook ({e}).") from e

    found = None
    for ws in wb.worksheets:
        hdr, fields, ledgers, rows = _find_header(ws)
        if hdr >= 0:
            found = (ws.title, hdr, fields, ledgers, rows)
            break
    if found is None:
        raise ValueError(
            "This is not a Tally Sales Register export: no header row with Date, Particulars, "
            f"Voucher Type and Voucher No. in the first {_HEADER_SCAN_ROWS} rows."
        )
    sheet, hdr, fields, ledgers, rows = found

    preamble = [_text(r[0]) if r else None for r in rows[:hdr]]
    company = next((p for p in preamble if p), None)
    period = next((p for p in preamble if p and _PERIOD.search(p)), None)

    def cell(row: tuple, field: str) -> Any:
        c = fields.get(field)
        return row[c] if c is not None and c < len(row) else None

    invoices: list[dict[str, Any]] = []
    grand_total: dict[str, Any] | None = None
    orphan_lines = 0
    cur: dict[str, Any] | None = None

    for row in rows[hdr + 1:]:
        if not row or all(v in (None, "") for v in row):
            continue
        particulars = _text(cell(row, "particulars"))
        if particulars and particulars.lower() == "grand total":
            grand_total = {
                "value": _num(cell(row, "value")),
                "gross_total": _num(cell(row, "gross_total")),
            }
            break
        voucher_no = _text(cell(row, "voucher_no"))
        when = _date(cell(row, "date"))
        if voucher_no and when is not None:
            narration = _text(cell(row, "narration"))
            reference = _text(cell(row, "reference"))
            replaced = _REPLACES.search(narration or "")
            cur = {
                "voucher_no": voucher_no,
                "voucher_type": _text(cell(row, "voucher_type")),
                "date": when,
                "party": particulars,
                "buyer": _text(cell(row, "buyer")),
                "buyer_address": _text(cell(row, "buyer_address")),
                "consignee": _text(cell(row, "consignee")),
                "consignee_address": _text(cell(row, "consignee_address")),
                "reference": reference,
                "quoted_so_number": so_number_of(reference),
                "narration": narration,
                "dispatch_from": dispatch_site(narration),
                "cartons": cartons(narration),
                "replaces_invoice": replaced.group(1) if replaced else None,
                "cancelled": bool(particulars and _CANCELLED.fullmatch(particulars)),
                "quantity": _num(cell(row, "quantity")),
                "alt_units": _num(cell(row, "alt_units")),
                "value": _num(cell(row, "value")),
                "gross_total": _num(cell(row, "gross_total")),
                "ledgers": {name: amt for c, name in ledgers.items()
                            if c < len(row) and (amt := _num(row[c])) is not None},
                "lines": [],
            }
            invoices.append(cur)
            continue
        if when is None and particulars:
            qty, alt = _num(cell(row, "quantity")), _num(cell(row, "alt_units"))
            rate, value = _num(cell(row, "rate")), _num(cell(row, "value"))
            if qty is None and alt is None and rate is None and value is None:
                continue  # a description row, not an item
            if cur is None:
                orphan_lines += 1
                continue
            cur["lines"].append({
                "line_no": len(cur["lines"]) + 1,
                "particulars": particulars,
                "quantity": qty,
                "alt_units": alt,
                "rate": rate,
                "value": value,
            })

    types = {(inv["voucher_type"] or "").strip().lower() for inv in invoices}
    if invoices and types <= {"sales order"}:
        raise ValueError(
            "This file holds Sales Orders, not invoices — upload it in the first section (Sales Order Book)."
        )
    if not invoices:
        raise ValueError("No vouchers found under the Sales Register header row.")

    return {
        "sheet": sheet,
        "company": company,
        "period": period,
        "ledger_columns": list(ledgers.values()),
        "invoices": invoices,
        "grand_total": grand_total,
        "orphan_lines": orphan_lines,
    }
