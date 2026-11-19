"""The Current stock download (Stock Take > Download), as changed on 19 Nov 2026.

- Qty (units) showed whole numbers as "2." and "-83.", because "#,##0.###"
  always prints the decimal point. Whole numbers now print as "2", the rest
  keep their decimals ("4.5", "-0.7").
- Only the columns the stock is read by are kept.
"""
from __future__ import annotations

from io import BytesIO

import openpyxl
import pytest

from app.modules.stock_take.services import export_xlsx as X


def _line(item, units, kg, floor="Second Floor Mezz", wh="W202", stock="Off Grade/Rejection"):
    return {"warehouse": wh, "floor_key": floor.upper(), "floor": floor, "item_key": item.upper(),
            "item_name": item, "item_type": "rm", "item_category": "seasoning",
            "item_subcategory": "seasoning", "stock_type": stock, "total_quantity": units,
            "counted_weight": 0.0, "net_adjustment_kg": kg, "total_weight": kg, "entry_count": 0,
            "transaction_count": 1, "last_counted_date": None, "days_since_count": None}


def _detail(rows):
    wb = openpyxl.load_workbook(BytesIO(X.build_stock_workbook(rows, {}, "Tester").read()))
    return wb, wb["Stock by floor"]


def test_the_detail_sheet_keeps_only_the_relevant_columns():
    _, ws = _detail([_line("JOWAR PUFF", -1, -5.0)])
    headers = [c.value for c in ws[1]]
    assert headers == ["Warehouse", "Floor", "Item", "Type", "Group", "Sub-group", "Stock type",
                       "Qty (units)", "Current stock (kg)"]
    assert ws.auto_filter.ref == "A1:I2"


def test_the_summary_keeps_only_the_relevant_columns():
    wb, _ = _detail([_line("JOWAR PUFF", -1, -5.0)])
    assert [c.value for c in wb["Summary"][6]] == [
        "Warehouse", "Floor", "Items", "Current stock (kg)", "Off grade (kg)"]


@pytest.mark.parametrize("units, value, fmt", [
    (2.0, 2, "#,##0"),
    (-83.0, -83, "#,##0"),
    (-1.0, -1, "#,##0"),
    (0.0, 0, "#,##0"),
    (-0.0, 0, "#,##0"),
    (21397.0, 21397, "#,##0"),
    (2.0000000001, 2, "#,##0"),           # float noise from summing is still whole
    (4.5, 4.5, "#,##0.0##"),
    (-0.7, -0.7, "#,##0.0##"),
    (1.125, 1.125, "#,##0.0##"),
    (None, 0, "#,##0"),
])
def test_units_never_end_in_a_bare_decimal_point(units, value, fmt):
    _, ws = _detail([_line("JOWAR PUFF", units, -5.0)])
    cell = ws.cell(row=2, column=8)
    assert cell.value == value and cell.number_format == fmt
    assert not cell.number_format.endswith(".###"), "that format prints 2 as '2.'"


def test_kg_keeps_two_decimals():
    _, ws = _detail([_line("WHITE COMPOUND CHOCOLATE", -1, -180.0)])
    cell = ws.cell(row=2, column=9)
    assert cell.value == -180.0 and cell.number_format == "#,##0.00"
