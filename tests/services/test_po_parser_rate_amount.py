"""A missing or unparseable Rate/Value must stay NULL, never become 0.

Regression origin: 357 of 357 uploaded lines landed with rate = 0.000 AND
amount = 0.000 (no NULLs). parser.py aliases the ZERO-returning
`safe_float_zero` as `_safe_float`, so a blank Col L / Col M -- or a Tally cell
carrying units, e.g. "25.00/Kg" -- silently becomes 0.0.

Why 0 is worse than NULL here:
  * the UI prints "Rs.0.00" instead of an em dash (lib/po.ts fmtCur returns "-"
    only for null), so a PO with no rate reads as a PO priced at zero;
  * po_diff.line_warnings treats 0.0 as a real rate and emits a bogus 100%
    rate-variance warning on every such line;
  * a genuine zero-rate line (free samples) becomes indistinguishable from a
    column the export simply did not carry.
"""
import io

import openpyxl
import pytest

from app.modules.purchase.services.parser import parse_po_book


def _book(*line_rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(11):
        ws.append([None])
    ws.append(["Date", "Particulars", "Voucher Type", "Voucher No."])

    header = [None] * 13
    header[0] = "1-Apr-25"
    header[1] = "SOME VENDOR"
    header[2] = "Purchase Order"
    header[3] = "CF/PO/2026-27/01228"
    header[12] = 325.0
    ws.append(header)

    for row in line_rows:
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _line_row(qty=13000, rate=None, amount=None, name="Salt & Pepper Seasoning"):
    row = [None] * 13
    row[1] = name
    row[9] = qty
    row[11] = rate
    row[12] = amount
    return row


def _only_line(**kw):
    parsed = parse_po_book(_book(_line_row(**kw)))
    assert len(parsed) == 1 and len(parsed[0]["lines"]) == 1
    return parsed[0]["lines"][0]


# ── the reported symptom ──────────────────────────────────────────────────

def test_blank_rate_is_null_not_zero():
    assert _only_line(rate=None)["rate"] is None


def test_blank_amount_is_null_not_zero():
    assert _only_line(amount=None)["amount"] is None


@pytest.mark.parametrize("cell", ["", "   ", "25.00/Kg", "N/A", "-"])
def test_unparseable_rate_is_null_not_zero(cell):
    """Tally can emit a unit-bearing or placeholder Rate cell. Guessing 0 turns
    'we could not read this' into 'the supplier charged nothing'."""
    assert _only_line(rate=cell)["rate"] is None


@pytest.mark.parametrize("cell", ["", "   ", "1,597.62", "Dr"])
def test_unparseable_amount_is_null_not_zero(cell):
    assert _only_line(amount=cell)["amount"] is None


# ── a real zero must survive as a real zero ───────────────────────────────

def test_explicit_zero_rate_is_preserved():
    """Free samples / no-charge lines are legitimate — 0 must stay 0, which is
    exactly the distinction the zero-fallback destroys."""
    assert _only_line(rate=0)["rate"] == 0.0


def test_explicit_zero_amount_is_preserved():
    assert _only_line(amount=0)["amount"] == 0.0


# ── normal values keep working ────────────────────────────────────────────

def test_numeric_rate_and_amount_still_parse():
    line = _only_line(rate=25.0, amount=325.0)
    assert line["rate"] == 25.0
    assert line["amount"] == 325.0


def test_numeric_strings_still_parse():
    line = _only_line(rate="25.0", amount="325.0")
    assert line["rate"] == 25.0
    assert line["amount"] == 325.0
