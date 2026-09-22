"""place_scope — which warehouse + floor a caller may act on, read off grants."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.stock_take import place_scope as P


class U:
    def __init__(self, warehouses, floors):
        self.allowed_warehouses = warehouses
        self.allowed_floors = floors


def test_no_grants_means_anywhere():
    assert P.assert_place_allowed(U([], []), "A185", "Ground Floor") is None
    assert P.assert_place_allowed(U(None, None), "W202", "Terrace") is None


def test_both_warehouse_spellings_are_the_same_building():
    P.assert_place_allowed(U(["W-202"], []), "W202", "Terrace")
    P.assert_place_allowed(U(["W202"], []), "W-202", "Terrace")


def test_another_warehouse_is_refused():
    with pytest.raises(HTTPException) as exc:
        P.assert_place_allowed(U(["A185"], []), "W-202", "First Floor")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "warehouse_not_allowed"


def test_a_floor_grant_matches_ignoring_case_and_padding():
    user = U(["W202"], [" Terrace "])
    P.assert_place_allowed(user, "W202", "terrace")
    with pytest.raises(HTTPException) as exc:
        P.assert_place_allowed(user, "W202", "First Floor")
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "floor_not_allowed"


def test_granted_normalises_and_dedupes():
    assert P.granted(U(["W-202", "W202", " "], [" terrace", "First Floor"])) == (
        ["W202"], ["FIRST FLOOR", "TERRACE"])
    assert P.granted(U(None, None)) == ([], [])
