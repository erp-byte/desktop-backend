"""A185 floors merged on 18 Nov 2026, and the floor shown under another name.

    FFS Packing Area       -> FG store
    Dmart Production Area  -> Dmart Packing Area   (shown as "Packing Area")
    Sorting Area           -> Dmart Packing Area

Stock Take only: it stops offering the merged floors and shows the new name,
while the stored names, the admin profile (FLOORS_BY_WAREHOUSE) and the read
scope Floor Requisition borrows all stay as they were.
"""
from __future__ import annotations

from io import BytesIO

import openpyxl
import pytest

from app.modules.stock_take import floors as F
from app.modules.stock_take import router as R
from app.modules.stock_take.services import export_xlsx as X
from app.modules.stock_take.services import transactions_service as T

MERGED = ["FFS Packing Area", "Dmart Production Area", "Sorting Area"]


class FakeUser:
    def __init__(self, warehouses, floors, is_admin=False):
        self.allowed_warehouses = warehouses
        self.allowed_floors = floors
        self.is_admin = is_admin


# Suraj Bhilare's profile: A185 and every floor this change touches.
SURAJ = FakeUser(["A185"], ["Dmart Packing Area", "FFS Packing Area", "FG store",
                            "Sorting Area", "Dmart Production Area", "Mezzanine"])


def test_merged_floors_are_recognised_whatever_the_spelling():
    assert F.is_merged_floor("A185", "FFS Packing Area")
    assert F.is_merged_floor("A-185", "  sorting area ")
    assert F.is_merged_floor("a185", "DMART PRODUCTION AREA")
    assert not F.is_merged_floor("A185", "FG store")
    assert not F.is_merged_floor("A185", "Dmart Packing Area")
    assert not F.is_merged_floor("W202", "Sorting Area"), "the merge is A185's alone"


def test_visible_floors_drops_only_the_merged_ones_and_keeps_order():
    assert F.visible_floors("A185", F.FLOORS_BY_WAREHOUSE["A185"]) == [
        f for f in F.FLOORS_BY_WAREHOUSE["A185"] if f not in MERGED]
    assert F.visible_floors("W202", F.FLOORS_BY_WAREHOUSE["W202"]) == F.FLOORS_BY_WAREHOUSE["W202"]


def test_dmart_packing_area_is_shown_as_packing_area_and_nothing_else_renamed():
    assert F.floor_display("A185", "Dmart Packing Area") == "Packing Area"
    assert F.floor_display("A-185", " DMART PACKING AREA ") == "Packing Area"
    assert F.floor_display("A185", "FG store") == "FG store"
    assert F.floor_display("W202", "Dmart Packing Area") == "Dmart Packing Area"
    assert F.floor_display("A185", None) == ""
    assert F.floor_labels() == {"Dmart Packing Area": "Packing Area"}


def test_the_admin_profile_is_untouched():
    """Other modules (admin screen, job cards) read FLOORS_BY_WAREHOUSE."""
    for f in MERGED:
        assert f in F.FLOORS_BY_WAREHOUSE["A185"]


def test_the_adjust_form_no_longer_offers_the_merged_floors():
    s = T.effective_scope(SURAJ)
    assert s["floors_by_warehouse"]["A185"] == ["Mezzanine", "Dmart Packing Area", "FG store"]
    assert not set(MERGED) & set(s["floors"])


def test_an_unrestricted_user_is_not_offered_them_either():
    s = T.effective_scope(FakeUser(["A185"], []))
    assert not set(MERGED) & set(s["floors_by_warehouse"]["A185"])
    assert "FG store" in s["floors_by_warehouse"]["A185"]


def test_posting_to_a_merged_floor_is_refused():
    with pytest.raises(T.ScopeError) as e:
        T.resolve_scope(SURAJ, warehouse="A185", location="Sorting Area")
    assert e.value.code == "floor_not_allowed"
    assert T.resolve_scope(SURAJ, warehouse="A185", location="dmart packing area") == (
        "A185", "Dmart Packing Area")


def test_the_read_scope_floor_requisition_borrows_is_unchanged():
    """Floor Requisition calls stock_take.router._clamp_to_read_scope, so hiding
    happens in the /filter-options response, not in _read_scope."""
    _, by_wh = R._read_scope(SURAJ, {"A185": ["FFS Packing Area", "FG store"]})
    assert "Sorting Area" in by_wh["A185"] and "FFS Packing Area" in by_wh["A185"]


def _line(floor, kg, units, item="HEALTHY CHOICE CHIA SEEDS 200 G"):
    return {"warehouse": "A185", "floor_key": floor.upper(), "floor": floor, "item_key": item,
            "item_name": item, "item_type": "fg", "item_category": "Seeds", "item_subcategory": "Chia",
            "stock_type": "Fresh Stock", "total_quantity": units, "counted_weight": kg,
            "net_adjustment_kg": 0.0, "total_weight": kg, "entry_count": 1, "transaction_count": 0,
            "last_counted_date": None, "days_since_count": None}


def _sheet_values(buf: BytesIO, sheet: str) -> list[tuple]:
    ws = openpyxl.load_workbook(buf)[sheet]
    return [tuple(r) for r in ws.iter_rows(values_only=True)]


def test_stock_download_leaves_out_empty_merged_floors_and_uses_the_new_name():
    rows = [_line("FG store", 3390.0, 17749.0), _line("FFS Packing Area", 0.0, 0.0),
            _line("Dmart Packing Area", 1000.0, 6500.0), _line("Sorting Area", 0.0, 0.0)]
    vals = _sheet_values(X.build_stock_workbook(rows, {}, "test"), "Stock by floor")
    floors = {v[1] for v in vals if v and v[0] and v[2] and v[1] not in ("Floor", None)}
    assert "Packing Area" in floors and "FG store" in floors
    assert not floors & {"FFS Packing Area", "Sorting Area", "Dmart Packing Area"}


def test_stock_download_keeps_a_merged_floor_that_still_holds_something():
    rows = [_line("Sorting Area", 0.0, 12.0)]
    vals = _sheet_values(X.build_stock_workbook(rows, {}, "test"), "Stock by floor")
    assert any(v[1] == "Sorting Area" for v in vals if v)


def test_ledger_and_entries_downloads_show_the_new_name():
    txn = {"txn_code": "26111801", "created_at": "2026-11-18T10:50:00", "warehouse": "A185",
           "location": "Dmart Packing Area", "item_name": "BLACK CHIA SEEDS", "operation": "ADDITION",
           "units": 1, "qty_kg": 1, "stock_type": "Fresh Stock", "reason": "x", "created_by": "y"}
    vals = _sheet_values(X.build_ledger_workbook([txn], {}, "test"), "Stock Transactions")
    assert any("Packing Area" in v for v in vals if v)
    assert not any("Dmart Packing Area" in v for v in vals if v)
    assert X._entry_cell({"warehouse": "A185", "floor_name": "Dmart Packing Area"}, "floor_name") == "Packing Area"
    assert X._entry_cell({"warehouse": "W202", "floor_name": "Terrace"}, "floor_name") == "Terrace"


def test_a_floor_named_without_its_warehouse_gets_the_display_name():
    assert F.floor_display_any("Dmart Packing Area") == "Packing Area"
    assert F.floor_display_any(" dmart packing area ", "A-185") == "Packing Area"
    assert F.floor_display_any("Dmart Packing Area", ["W202"]) == "Dmart Packing Area"
    assert F.floor_display_any("Terrace", ["A185", "W202"]) == "Terrace"
    assert F.floor_display_any(None) == ""


def test_download_headers_name_the_floor_as_shown():
    """The filter value is the stored name; the header above the rows must not
    say 'Dmart Packing Area' while the rows say 'Packing Area'."""
    stock = X._describe_stock({"warehouse": ["A185"], "floorName": ["Dmart Packing Area"]})
    assert "Floor: Packing Area" in stock and "Dmart" not in stock
    ledger = X._describe({"warehouse": "A185", "location": "Dmart Packing Area"})
    assert "Floor: Packing Area" in ledger and "Dmart" not in ledger
    assert "Floor: Terrace" in X._describe({"warehouse": "W202", "location": "Terrace"})


def test_a_merged_floor_line_that_only_nets_to_zero_is_left_off():
    """Counted +135 and adjusted -135: 0 kg and 0 units now. The sheet has no
    Counted or Adjustments column any more, so there is nothing to keep it for."""
    line = {**_line("FFS Packing Area", 0.0, 0.0), "counted_weight": 135.0, "net_adjustment_kg": -135.0}
    assert X._empty_merged(line)
    assert not X._empty_merged(_line("FFS Packing Area", 0.0, 3.0)), "units left: keep it"
    vals = _sheet_values(X.build_stock_workbook([line, _line("FG store", 10.0, 1.0)], {}, "test"), "Stock by floor")
    assert not any(v[1] == "FFS Packing Area" for v in vals if v)
