"""The Stock Take LIST is scoped to the caller's profile.

_read_scope answers "what may I SEE", which is NOT the same question /scope
answers. /scope offers the floors the ERP declares, because you should not be
able to file an adjustment against TEESTTTT. A read filter built from that list
would hide real counted stock: 301 W202 rows sit on STORE, a floor nobody
declared. So the read scope comes from where stock is actually RECORDED --
plus the declared floors, because a declared floor can hold nothing yet, and a
granted floor missing from this list is refused outright by /latest-stock.
"""
from __future__ import annotations

import pytest

from app.modules.stock_take import router as R

# What the table holds, shaped like fetch_places() returns it.
PLACES = {
    "W202": ["First Floor", "Lower Basement", "STORE", "TEESTTTT", "Terrace"],
    "A185": ["A185 Stores", "COLD", "Mezzanine"],
    "F53":  ["GROUND FLOOR", "STORE AREA"],
}


class FakeUser:
    def __init__(self, warehouses, floors, is_admin=False):
        self.allowed_warehouses = warehouses
        self.allowed_floors = floors
        self.is_admin = is_admin


def test_no_grants_means_everything_not_nothing():
    """auth_schema.sql:35 — an empty list is 'no restriction', not 'no access'."""
    whs, by_wh = R._read_scope(FakeUser([], []), PLACES)
    assert whs == ["A185", "F53", "W202"]
    assert by_wh["W202"][:len(PLACES["W202"])] == PLACES["W202"]
    assert "Second Floor" in by_wh["W202"], "declared, even with no rows"


# Suraj Bhilare's live profile on 2026-09-15: A185, five floors.
SURAJ = FakeUser(["A185"], ["Dmart Packing Area", "FFS Packing Area", "FG store",
                            "Sorting Area", "Dmart Production Area"])


def test_a_granted_floor_holding_nothing_is_still_nameable():
    """The Adjust screen offered Dmart Production Area (from /scope, which reads
    the declared profile), let him post to it, and then /latest-stock refused
    it: "You are not assigned to floor 'Dmart Production Area'". The floor held
    no rows after the 13 Sep restart, so `places` did not list it."""
    _, by_wh = R._read_scope(SURAJ, PLACES)
    assert "Dmart Production Area" in by_wh["A185"]
    assert sorted(by_wh["A185"]) == sorted(SURAJ.allowed_floors)


def test_declared_floors_never_widen_a_floor_grant():
    """Adding the declared list must not hand out floors the profile withholds."""
    _, by_wh = R._read_scope(FakeUser(["A185"], ["Mezzanine"]), PLACES)
    assert by_wh["A185"] == ["Mezzanine"]


def test_a_declared_floor_is_not_listed_twice_under_another_spelling():
    """The data says STORE, W202 declares Store: one floor, one entry."""
    _, by_wh = R._read_scope(FakeUser(["W202"], []), PLACES)
    assert [f for f in by_wh["W202"] if f.upper() == "STORE"] == ["STORE"]


def test_a_warehouse_grant_narrows_the_list():
    whs, by_wh = R._read_scope(FakeUser(["W202"], []), PLACES)
    assert whs == ["W202"]
    assert "A185" not in by_wh


def test_undeclared_floors_stay_visible():
    """The whole reason this is not the posting scope.

    STORE holds 301 W202 rows and is in no warehouse's declared floor list. A
    caller who may see W202 must be able to name it, or that stock is
    unreachable from the console rather than merely unpostable.
    """
    _, by_wh = R._read_scope(FakeUser(["W202"], []), PLACES)
    assert "STORE" in by_wh["W202"]
    assert "TEESTTTT" in by_wh["W202"], "even the junk, while rows still carry it"


def test_a_floor_grant_restricts_within_the_warehouse():
    whs, by_wh = R._read_scope(FakeUser(["W202"], ["Terrace", "First Floor"]), PLACES)
    assert whs == ["W202"]
    assert by_wh["W202"] == ["First Floor", "Terrace"]
    assert "STORE" not in by_wh["W202"]


def test_floor_matching_ignores_case_and_padding():
    """The floor app writes trailing spaces; grants are typed by hand."""
    _, by_wh = R._read_scope(FakeUser(["W202"], ["  terrace  "]), PLACES)
    assert by_wh["W202"] == ["Terrace"]


def test_hyphenated_warehouse_codes_resolve():
    """allowed_warehouses carries both W202 and W-202 for the same building."""
    whs, _ = R._read_scope(FakeUser(["W-202"], []), PLACES)
    assert whs == ["W202"]


def test_a_granted_warehouse_with_no_floors_of_its_own_is_seen_whole():
    """Changed deliberately on 2026-09-18 (this test used to pin F53 -> []).

    F53 declares no floors, so the admin screen had none to offer, and the
    grant "Terrace" cannot have been about it. Granted by name, it is the
    caller's in full. The case that forced the decision: an admin granted
    D-39 plus W202/A185 floors saw none of Savla's 312,067 kg.
    """
    whs, by_wh = R._read_scope(FakeUser(["F53"], ["Terrace"]), PLACES)
    assert whs == ["F53"]
    assert by_wh["F53"] == PLACES["F53"]


def test_a_declared_warehouse_is_still_narrowed_to_nothing_by_other_floors():
    """A185 declares floors, so a W202-only floor grant still leaves it empty."""
    _, by_wh = R._read_scope(FakeUser(["A185", "W202"], ["Terrace"]), PLACES)
    assert by_wh["A185"] == []
    assert by_wh["W202"] == ["Terrace"]


def test_a_grant_that_names_an_undeclared_warehouse_floor_narrows_it():
    """Set directly in the table (user 164 holds Savla, Savla Bond, ...): a floor
    grant that names one of F53's recorded floors is about F53, so it narrows."""
    _, by_wh = R._read_scope(FakeUser(["F53"], ["Store Area"]), PLACES)
    assert by_wh["F53"] == ["STORE AREA"]


def test_without_a_warehouse_grant_floor_grants_narrow_everything():
    """Nothing was granted by name, so nothing is seen whole."""
    _, by_wh = R._read_scope(FakeUser([], ["Terrace"]), PLACES)
    assert by_wh["F53"] == [] and by_wh["W202"] == ["Terrace"]


def test_being_admin_does_not_widen_the_read_scope():
    """Admins bypass ENFORCEMENT (middleware.py:160); that is not the same as
    having no profile. A dropdown is a suggestion, and an admin assigned W202
    being shown A185's floors is simply a wrong menu."""
    whs, _ = R._read_scope(FakeUser(["W202"], [], is_admin=True), PLACES)
    assert whs == ["W202"]


@pytest.mark.parametrize("floors", [None, [], ["   "]])
def test_blank_floor_grants_are_treated_as_no_grant(floors):
    _, by_wh = R._read_scope(FakeUser(["W202"], floors), PLACES)
    _, one_floor = R._read_scope(FakeUser(["W202"], ["First Floor"]), PLACES)
    assert by_wh["W202"][:len(PLACES["W202"])] == PLACES["W202"]
    assert len(by_wh["W202"]) > len(one_floor["W202"])


def test_a_warehouse_absent_from_the_data_still_appears_with_no_floors():
    """Granted A68, which holds nothing. It must not vanish silently -- an empty
    floor list is the honest answer, and is what tells the UI to say so."""
    whs, by_wh = R._read_scope(FakeUser(["A68"], []), PLACES)
    assert whs == ["A68"]
    assert by_wh["A68"] == []


# ── The response model must accept what the endpoint actually returns ──
#
# /filter-options shipped broken for exactly this reason: it returned the nested
# floors_by_warehouse map while still annotated `-> dict[str, list[str]]`.
# FastAPI derives a response MODEL from that annotation and validates against it,
# so every call 500'd. The browser treats a failed filter-options as non-fatal —
# the dropdowns just stay empty — so nothing surfaced except four empty controls.

from fastapi.routing import APIRoute  # noqa: E402

from app.modules.stock_take.router import router as st_router  # noqa: E402


def _route(suffix: str) -> APIRoute:
    for r in st_router.routes:
        if isinstance(r, APIRoute) and r.path.endswith(suffix):
            return r
    raise AssertionError("no route ending %r" % suffix)


def test_filter_options_response_model_accepts_the_nested_floor_map():
    payload = {
        "warehouses": ["A185", "W202"],
        "floors": ["First Floor", "Mezzanine"],
        "item_types": ["RM", "FG"],
        "stock_types": ["Fresh Stock"],
        "floors_by_warehouse": {"W202": ["First Floor"], "A185": ["Mezzanine"]},
    }
    _, errors = _route("/filter-options").response_field.validate(
        payload, {}, loc=("response",))
    assert not errors, errors


def test_scope_response_model_accepts_its_nested_floor_map():
    """Same shape, same trap — /scope grew floors_by_warehouse at the same time."""
    payload = {
        "warehouses": ["W202"],
        "floors": ["First Floor"],
        "floors_by_warehouse": {"W202": ["First Floor"]},
        "warehouses_unrestricted": False,
        "floors_unrestricted": True,
        "can_post": True,
        "blocked_reason": None,
    }
    _, errors = _route("/scope").response_field.validate(payload, {}, loc=("response",))
    assert not errors, errors
