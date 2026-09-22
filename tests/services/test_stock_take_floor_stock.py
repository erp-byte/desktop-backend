"""GET /stock-take/floor-stock — which place a caller may read, and the empty-table case.

The job card's "Material allocation and requisition" tab asks for everything on
ONE warehouse + floor: the job card's own. The figures themselves are proven
against the live table by test_stock_take_floor_stock_live_sql.py; this file
covers what needs no database.

The scope rule is read straight off the caller's grants, not off `places` (the
floors that already hold counts). A job card's floor can be legitimately empty —
nothing counted there yet — and refusing it would report a permissions problem
where the truth is "no stock recorded here".

Run:  PYTHONPATH=. .venv/Scripts/python.exe -m pytest -q tests/services/test_stock_take_floor_stock.py
"""
from __future__ import annotations

import asyncio

import asyncpg
import pytest
from fastapi import HTTPException

from app.modules.stock_take import router as R
from app.modules.stock_take.services import floor_stock_service as svc


class FakeUser:
    def __init__(self, warehouses, floors, is_admin=False):
        self.allowed_warehouses = warehouses
        self.allowed_floors = floors
        self.is_admin = is_admin


def test_an_unrestricted_caller_reads_any_place_normalised():
    """Job cards spell the plant 'W-202'; the entries table only ever has 'W202'."""
    assert R._floor_stock_place(FakeUser([], []), "W-202", "  First Floor ") == ("W202", "First Floor")


def test_a_place_with_no_counts_yet_is_not_refused():
    """Empty grants mean 'no restriction' (auth_schema.sql:35) — even where nothing is counted."""
    assert R._floor_stock_place(FakeUser([], []), "A68", "Ground Floor") == ("A68", "Ground Floor")


def test_a_warehouse_grant_refuses_another_building():
    with pytest.raises(HTTPException) as exc:
        R._floor_stock_place(FakeUser(["A185"], []), "W-202", "First Floor")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "warehouse_not_allowed"


def test_a_hyphenated_grant_matches_the_unhyphenated_code():
    """allowed_warehouses carries both spellings for the same building."""
    assert R._floor_stock_place(FakeUser(["W-202"], []), "W202", "Terrace") == ("W202", "Terrace")


def test_a_floor_grant_restricts_within_the_warehouse_case_insensitively():
    user = FakeUser(["W202"], [" Terrace "])
    assert R._floor_stock_place(user, "W202", "terrace") == ("W202", "terrace")
    with pytest.raises(HTTPException) as exc:
        R._floor_stock_place(user, "W202", "First Floor")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "floor_not_allowed"


@pytest.mark.parametrize("warehouse, floor", [("", "First Floor"), ("W202", "   "), (None, None)])
def test_a_place_is_required(warehouse, floor):
    """No warehouse or no floor is a malformed request, never 'the whole building'."""
    with pytest.raises(HTTPException) as exc:
        R._floor_stock_place(FakeUser([], []), warehouse, floor)
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "place_required"


def test_a_missing_entries_table_is_an_empty_floor_not_a_500():
    """The Supabase config carries no stock-take tables: an empty answer is the honest one."""

    class Conn:
        async def fetch(self, *args, **kwargs):
            raise asyncpg.UndefinedTableError('relation "new_stock_entries" does not exist')

    out = asyncio.run(svc.fetch_floor_stock(Conn(), warehouse="W202", floor="First Floor"))
    assert out == {"warehouse": "W202", "floor": "First Floor", "items": []}


def test_a_row_carries_the_pack_weight_as_a_number_and_the_date_as_text():
    """unit_uom holds kg per unit (0.050, 1.000, 0.000 = not recorded), NOT a unit
    name. It reaches the browser as a number, like every other figure."""
    from datetime import date
    from decimal import Decimal

    row = {
        "item_name": "PM24-GOOD LIFE CHIA SEEDS 100G POUCH", "item_type": "pm",
        "item_category": None, "item_subcategory": None, "stock_type": "Fresh Stock",
        "unit_uom": Decimal("0.050"),
        "counted_quantity": Decimal("8970.00"), "counted_weight": Decimal("897.00"),
        "net_adjustment_kg": Decimal("0"), "net_adjustment_units": Decimal("0"),
        "available_quantity": Decimal("8970.00"), "available_kg": Decimal("897.00"),
        "last_counted_date": date(2026, 9, 14), "entry_count": 2, "txn_count": 0,
    }
    out = svc._row(row)
    assert out["unit_uom"] == 0.05 and isinstance(out["unit_uom"], float)
    assert out["available_quantity"] == 8970.0
    assert out["last_counted_date"] == "2026-09-14"
    assert svc._row({**row, "unit_uom": None, "last_counted_date": None})["unit_uom"] is None
