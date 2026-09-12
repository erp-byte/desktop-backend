"""The Stock Take LIST is scoped to the caller's profile.

_read_scope answers "what may I SEE", which is NOT the same question /scope
answers. /scope offers the floors the ERP declares, because you should not be
able to file an adjustment against TEESTTTT. A read filter built from that list
would hide real counted stock: 301 W202 rows sit on STORE, a floor nobody
declared. So the read scope comes from where stock is actually RECORDED.
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
    assert by_wh["W202"] == PLACES["W202"]


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


def test_a_granted_warehouse_with_no_matching_floor_grant_comes_back_empty():
    """Documents today's behaviour, which is a real edge worth seeing.

    A caller granted F53 plus only W202 floor names gets F53 with an empty floor
    list -- the warehouse is theirs but no floor in it is. Whether that should
    instead mean 'unrestricted within F53' is a policy question, not a bug; this
    test exists so changing it is a deliberate act rather than a surprise.
    """
    whs, by_wh = R._read_scope(FakeUser(["F53"], ["Terrace"]), PLACES)
    assert whs == ["F53"]
    assert by_wh["F53"] == []


def test_being_admin_does_not_widen_the_read_scope():
    """Admins bypass ENFORCEMENT (middleware.py:160); that is not the same as
    having no profile. A dropdown is a suggestion, and an admin assigned W202
    being shown A185's floors is simply a wrong menu."""
    whs, _ = R._read_scope(FakeUser(["W202"], [], is_admin=True), PLACES)
    assert whs == ["W202"]


@pytest.mark.parametrize("floors", [None, [], ["   "]])
def test_blank_floor_grants_are_treated_as_no_grant(floors):
    _, by_wh = R._read_scope(FakeUser(["W202"], floors), PLACES)
    assert by_wh["W202"] == PLACES["W202"]


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
