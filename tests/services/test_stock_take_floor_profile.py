"""The Python floor profile must equal the TypeScript one, exactly.

app/modules/stock_take/floors.py duplicates FLOORS_BY_WAREHOUSE from
web_replica/src/lib/admin-api.ts. A duplicate that nothing checks will drift --
someone adds a floor on the admin screen, the dropdown offers it, and the server
rejects every post to it with "You are not assigned to floor". This parses the
real .ts file and compares.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.modules.stock_take import floors as F

TS = (Path(__file__).resolve().parents[3] / "web_replica" / "src" / "lib" / "admin-api.ts")


def _parse_ts_map() -> dict[str, list[str]]:
    """Pull FLOORS_BY_WAREHOUSE out of admin-api.ts without a JS engine."""
    src = TS.read_text(encoding="utf-8")
    m = re.search(r"FLOORS_BY_WAREHOUSE:\s*Record<string,\s*string\[\]>\s*=\s*\{", src)
    assert m, "FLOORS_BY_WAREHOUSE not found in admin-api.ts"
    i = src.index("{", m.end() - 1)
    depth, j = 0, i
    while j < len(src):                     # find the matching close brace
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = src[i:j + 1]
    body = re.sub(r"//[^\n]*", "", body)    # strip the comments we added
    body = re.sub(r",(\s*[}\]])", r"\1", body)   # trailing commas -> valid literal
    return ast.literal_eval(body)


@pytest.fixture(scope="module")
def ts_map() -> dict[str, list[str]]:
    if not TS.exists():
        pytest.skip("web_replica is not checked out next to server_replica")
    return _parse_ts_map()


def test_python_profile_matches_typescript(ts_map):
    assert F.FLOORS_BY_WAREHOUSE == ts_map, (
        "floors.py and admin-api.ts disagree. Add the floor in BOTH.\n"
        f"  python only: {set(sum(F.FLOORS_BY_WAREHOUSE.values(), [])) - set(sum(ts_map.values(), []))}\n"
        f"  ts only    : {set(sum(ts_map.values(), [])) - set(sum(F.FLOORS_BY_WAREHOUSE.values(), []))}"
    )


def test_the_three_areas_added_on_2026_09_09_are_present(ts_map):
    """These carry 437,006 kg that had no declared home until they were added."""
    for f in ("A185 Stores", "A185 Stores Rack", "A185 Cold"):
        assert f in ts_map["A185"], f
        assert f in F.FLOORS_BY_WAREHOUSE["A185"], f


def test_declared_floors_is_order_preserving_and_deduped():
    got = F.declared_floors(["W202", "A185", "W202"])
    assert got[:2] == ["Lower Basement", "Upper Basement"], "declaration order, not sorted"
    assert len(got) == len(set(got)), "a warehouse listed twice must not duplicate floors"
    # Derived, not pinned: a magic total means every new floor edits this test,
    # which trains you to change the number rather than read the assertion. The
    # sum still fails loudly if two warehouses ever declare the same floor name.
    assert len(got) == len(F.FLOORS_BY_WAREHOUSE["W202"]) + len(F.FLOORS_BY_WAREHOUSE["A185"])


def test_hyphenated_warehouse_codes_resolve():
    """allowed_warehouses carries both W202 and W-202 for the same building."""
    assert F.declared_floors(["W-202"]) == F.declared_floors(["W202"])
    assert F.normalise_warehouse(" a-185 ") == "A185"


def test_undeclared_warehouses_fall_back_to_their_own_data():
    """F53 declares nothing, so refusing to fall back would empty its dropdown."""
    assert F.undeclared(["W202", "F53"]) == ["F53"]
    assert F.floors_for(["F53"], ["GROUND FLOOR", "STORE AREA"]) == ["GROUND FLOOR", "STORE AREA"]


def test_a_declared_warehouse_is_never_widened_by_the_data():
    """The whole point: TEESTTTT is in the table and must not reach a dropdown."""
    got = F.floors_for(["W202"], ["TEESTTTT", "1ST : FIRST LINE", "STORE"])
    assert got == F.declared_floors(["W202"])
    assert "TEESTTTT" not in got
