"""floor_requisition.rules — units, quantities, the snapshot, the actor."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.floor_requisition import rules as R


@pytest.mark.parametrize("uom, item_type, want", [
    ("KGS", "RM", "kg"),
    ("PCS", "PM", "pcs"),
    ("Nos", None, "pcs"),
    (None, "pm", "pcs"),
    (None, "RM", "kg"),
    ("bags", "PM", "pcs"),   # unrecognised uom falls back to the type
    ("", None, "kg"),
])
def test_unit_for(uom, item_type, want):
    assert R.unit_for(uom, item_type) == want


@pytest.mark.parametrize("value, unit, want", [
    ("88.2", "kg", Decimal("88.200")),
    (88.2, "kg", Decimal("88.200")),
    ("0.001", "kg", Decimal("0.001")),
    ("1000", "pcs", Decimal("1000.000")),
    ("10.000", "pcs", Decimal("10.000")),
    ("99999999999.999", "kg", Decimal("99999999999.999")),
])
def test_parse_qty_accepts(value, unit, want):
    assert R.parse_qty(value, unit) == want


@pytest.mark.parametrize("value, unit, message", [
    ("1.2345", "kg", "Kilograms go to 3 decimals at most."),
    ("10.5", "pcs", "Pieces are whole numbers."),
    ("0", "kg", "The quantity must be more than 0."),
    ("-3", "pcs", "The quantity must be more than 0."),
    ("abc", "kg", "Enter a quantity as a number."),
    (None, "kg", "Enter a quantity as a number."),
    (True, "pcs", "Enter a quantity as a number."),
    ("NaN", "kg", "Enter a quantity as a number."),
    ("1e11", "kg", "That quantity is too large."),
    ("1e40", "pcs", "That quantity is too large."),
])
def test_parse_qty_refuses(value, unit, message):
    with pytest.raises(R.QtyError) as exc:
        R.parse_qty(value, unit)
    assert str(exc.value) == message


def _stock(kg, stock_type="Fresh Stock", pcs=0):
    return {"stock_type": stock_type, "available_kg": kg, "available_quantity": pcs}


def test_snapshot_kg_against_fresh_stock_only():
    s = R.snapshot(unit="kg", indent_lines=[("KGS", "250.000", "240.000")],
                   stock=[_stock(161.8), _stock(2.89, "Off Grade/Rejection")])
    assert s == R.Snapshot("kg", Decimal("250.000"), Decimal("161.800"), Decimal("88.200"))


def test_snapshot_pcs_reads_the_piece_count():
    s = R.snapshot(unit="pcs", indent_lines=[("PCS", 1000, 1000)], stock=[_stock(120, pcs=1200)])
    assert s == R.Snapshot("pcs", Decimal("1000.000"), Decimal("1200.000"), Decimal("0.000"))


def test_snapshot_without_an_indent_line_has_no_requirement_or_shortage():
    s = R.snapshot(unit="kg", indent_lines=[], stock=[_stock(5)])
    assert s == R.Snapshot("kg", None, Decimal("5.000"), None)


def test_snapshot_nothing_on_the_floor():
    s = R.snapshot(unit="pcs", indent_lines=[("PCS", 25, 25)], stock=[])
    assert s == R.Snapshot("pcs", Decimal("25.000"), Decimal("0.000"), Decimal("25.000"))


def test_snapshot_sums_lines_skips_other_units_and_falls_back_to_reqd():
    s = R.snapshot(unit="kg", indent_lines=[
        ("KGS", "10", "9"), ("kgs", None, "5.5"), ("PCS", "400", "400"), ("KGS", "x", None),
    ], stock=[])
    assert s.required == Decimal("15.500")


def test_snapshot_rounds_to_three_places():
    s = R.snapshot(unit="kg", indent_lines=[("KGS", "0.3", None)], stock=[_stock(0.1), _stock(0.2)])
    assert (s.available, s.shortage) == (Decimal("0.300"), Decimal("0.000"))


class FakeUser:
    def __init__(self, full_name="", email="", phone="", user_id=7):
        self.full_name, self.email, self.phone, self.user_id = full_name, email, phone, user_id


def test_actor_name_prefers_name_then_email_then_phone_then_id():
    assert R.actor_name(FakeUser(full_name="Ravi K", email="r@x")) == "Ravi K"
    assert R.actor_name(FakeUser(email="r@x", phone="98")) == "r@x"
    assert R.actor_name(FakeUser(phone="98")) == "98"
    assert R.actor_name(FakeUser()) == "user:7"
