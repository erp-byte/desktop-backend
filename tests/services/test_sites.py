"""The shared site table (app/core/sites.py + sites.json) and the warehouse lock
rebuilt on it (warehouse_scope.user_has_warehouse).

Before it, the backend's only comparator folded case and punctuation: a store
user granted 'D-39' was refused on every request from 'Savla D-39', a box
received at 'Warehouse A185' could not leave from 'A185', and 'Supreme Cold'
was not 'Supreme'. These pin one answer per spelling, the user's decisions of
2026-10-16 ('Cold Storage' and 'Savla Bond' are sites of their own, bare
'Savla' is unknown, 'Savla D-34' is Savla D-39), and the frontend copy of the
table (frontend/src/lib/sites.json, checked by src/lib/sites.test.ts with the
same cases).

Run:  PYTHONPATH=. python -m pytest tests/services/test_sites.py -q
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core import sites as S
from app.core.warehouse_scope import (
    WAREHOUSE_NORM_ANY_SQL,
    normalise_warehouse_code,
    user_has_any_warehouse,
    user_has_warehouse,
)

BACKEND_JSON = Path(S.__file__).with_name("sites.json")
FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
FRONTEND_JSON = FRONTEND / "src" / "lib" / "sites.json"
TABLE = json.loads(BACKEND_JSON.read_text(encoding="utf-8"))["sites"]


# ── The table itself ───────────────────────────────────────────────────────

def test_the_frontend_copy_is_byte_identical():
    if not FRONTEND.is_dir():
        pytest.skip("no frontend checkout next to the backend")
    assert FRONTEND_JSON.is_file(), f"missing {FRONTEND_JSON}"
    assert FRONTEND_JSON.read_bytes() == BACKEND_JSON.read_bytes(), \
        "frontend/src/lib/sites.json differs from backend app/core/sites.json; copy one over the other"


def test_no_key_belongs_to_two_sites_and_no_alias_is_redundant():
    owner: dict[str, str] = {}
    for row in TABLE:
        names = [row["canonical"], row["display"], row["grant_code"]]
        own = {normalise_warehouse_code(n) for n in names if n}
        for alias in row["aliases"]:
            key = normalise_warehouse_code(alias)
            assert key, f"{row['canonical']}: alias {alias!r} has no key"
            assert key not in own, \
                f"{row['canonical']}: alias {alias!r} adds nothing (same key as another spelling)"
            own.add(key)
        for key in own:
            assert owner.setdefault(key, row["canonical"]) == row["canonical"], \
                f"{key} is claimed by {owner[key]} and {row['canonical']}"


def test_every_row_is_well_formed():
    assert [r["canonical"] for r in TABLE] == [
        "W202", "A185", "A101", "A68", "F53", "Rishi", "Savla D-39", "Savla D-514",
        "Savla Bond", "Supreme", "Eskimo", "Cold Storage", "Dev Int",
    ]
    for row in TABLE:
        assert set(row) == {"canonical", "kind", "company", "grant_code", "display",
                            "address", "destination", "aliases"}, row["canonical"]
        assert row["kind"] in {"warehouse", "cold", "unknown"}
        if row["kind"] == "cold" and row["canonical"] != "Cold Storage":
            assert row["company"] in {"cfpl", "cdpl"}, row["canonical"]
            assert row["destination"] and row["grant_code"], row["canonical"]
        else:
            assert row["company"] is None, row["canonical"]


def test_bare_savla_is_not_in_the_table():
    """It names no one store: resolve it by the cold row's unit, else leave it unknown."""
    assert S.canonical_site("Savla") == "Savla"
    assert S.site_kind("SAVLA") == "unknown"
    assert not S.is_cold_site("Savla")


# ── Every spelling resolves to its site ────────────────────────────────────

SPELLINGS = {
    "W202": ["W202", "W-202", "w 202", "Warehouse W202", "warehouse w-202"],
    "A185": ["A185", "A-185", "Warehouse A185", "A185 Cold", "A-185 Cold", "a-185 cold"],
    "A101": ["A101", "A-101", "Warehouse A101"],
    "A68": ["A68", "A-68", "Warehouse A68"],
    "F53": ["F53", "Warehouse F53", "APMC F53", "apmc-f53"],
    "Rishi": ["Rishi", "RISHI", "Rishi Cold", "Rishi Cold Storage", "Rishi Cold Storage Pvt Ltd"],
    "Savla D-39": ["Savla D-39", "savla d39", "savla-d39", "savla-d-39", "D-39", "D39",
                   "Savla D-39 Cold", "savla d39 cold", "Savla D-39 Cold Storage",
                   "Old Savla", "old_savla", "OLD SAVLA", "Savla D-34"],
    "Savla D-514": ["Savla D-514", "savla d514", "savla-d-514", "D-514", "D514",
                    "Savla D-514 Cold", "Savla D-514 Cold Storage", "New Savla", "new_savla"],
    "Savla Bond": ["Savla Bond", "SAVLA BOND", "savla_bond"],
    "Supreme": ["Supreme", "SUPREME", "Supreme Cold", "Supreme Cold Storage"],
    "Eskimo": ["Eskimo", "ESKIMO", "Eskimo Cold", "Eskimo Cold Storage"],
    "Cold Storage": ["Cold Storage", "COLD STORAGE", "cold-storage"],
    "Dev Int": ["Dev Int", "dev_int", "DEV INT"],
}


@pytest.mark.parametrize("canonical,raw", [(c, r) for c, rs in SPELLINGS.items() for r in rs])
def test_every_spelling_resolves(canonical, raw):
    assert S.canonical_site(raw) == canonical
    assert S.canonical_site(f"  {raw}  ") == canonical
    assert S.same_site(raw, canonical)


@pytest.mark.parametrize("row", TABLE, ids=lambda r: r["canonical"])
def test_every_listed_spelling_resolves(row):
    for raw in [row["canonical"], row["display"], row["grant_code"], *row["aliases"]]:
        if raw:
            assert S.canonical_site(raw) == row["canonical"], raw


def test_an_unknown_name_comes_back_trimmed():
    assert S.canonical_site("  Some Godown ") == "Some Godown"
    assert S.canonical_site("") == "" and S.canonical_site(None) == ""
    assert S.display_site(" Some Godown ") == "Some Godown"
    assert S.site_address("Some Godown") == ""


# ── same_site ──────────────────────────────────────────────────────────────

SAME = [
    ("Warehouse A185", "A185", True), ("A-185", "A185", True), ("A185 Cold", "A185", True),
    ("D-39", "Savla D-39", True), ("Savla D39", "Savla D-39", True), ("Savla D-39 Cold", "Savla D-39", True),
    ("Savla D-34", "Savla D-39", True), ("Old Savla", "D-39", True), ("New Savla", "D-514", True),
    ("Supreme Cold", "Supreme", True), ("Supreme Cold Storage", "Supreme", True),
    ("ESKIMO", "Eskimo", True), ("Rishi Cold Storage Pvt Ltd", "Rishi", True), ("W-202", "Warehouse W202", True),
    ("Dev Int", "dev_int", True), ("Savla Bond", "savla bond", True), ("Cold Storage", "cold storage", True),
    ("Savla Bond", "Savla D-39", False), ("Savla Bond", "D-39", False), ("Savla D-39", "Savla D-514", False),
    ("Cold Storage", "Rishi", False), ("Cold Storage", "Savla D-39", False), ("Cold Storage", "Supreme", False),
    ("Savla", "Savla D-39", False), ("Savla", "Savla D-514", False), ("W202", "A185", False), ("A185", "A101", False),
    ("Some Godown", "some-godown", True), ("Some Godown", "Other Godown", False),
    ("", "", False), ("", "A185", False), ("A185", "", False), (None, None, False), ("  ", "  ", False),
]


@pytest.mark.parametrize("a,b,want", SAME)
def test_same_site(a, b, want):
    assert S.same_site(a, b) is want
    assert S.same_site(b, a) is want


def test_same_site_never_refuses_what_the_old_comparator_allowed():
    """Production locks compared normalise_warehouse_code(a) == ...(b). Every pair
    that matched that way must still match: the table only adds matches."""
    pool = [r for rs in SPELLINGS.values() for r in rs] + ["Store", "store", "W 202", "Some Godown"]
    for a in pool:
        for b in pool:
            if normalise_warehouse_code(a) == normalise_warehouse_code(b):
                assert S.same_site(a, b), (a, b)


# ── Kind, company, display, address ────────────────────────────────────────

@pytest.mark.parametrize("raw", ["Rishi", "Savla D-39", "D-514", "Savla Bond", "Supreme Cold",
                                 "Eskimo", "Cold Storage"])
def test_cold_sites_are_cold(raw):
    assert S.is_cold_site(raw)


@pytest.mark.parametrize("raw", ["W202", "A-185", "A185 Cold", "F53", "Dev Int", "Savla", "", None])
def test_warehouses_floors_and_unknown_names_are_not_cold(raw):
    assert not S.is_cold_site(raw)


@pytest.mark.parametrize("raw,company", [
    ("Savla D-39", "cfpl"), ("D-514", "cfpl"), ("Savla Bond", "cfpl"), ("Rishi", "cdpl"),
    ("Supreme Cold", "cdpl"), ("ESKIMO", "cdpl"), ("Cold Storage", None), ("W202", None),
    ("Savla", None), ("", None),
])
def test_cold_company(raw, company):
    assert S.cold_company(raw) == company


def test_kinds():
    assert [S.site_kind(r) for r in ["W-202", "Savla Bond", "Cold Storage", "Dev Int", "Savla", ""]] == \
        ["warehouse", "cold", "cold", "unknown", "unknown", "unknown"]


def test_display_names_and_addresses():
    assert S.display_site("Supreme") == "Supreme Cold Storage"
    assert S.display_site("D-39") == "Savla D-39 Cold Storage"
    assert S.display_site("W-202") == "Warehouse W202"
    assert S.display_site("Savla Bond") == "Savla Bond"
    assert S.display_site("") == ""
    assert S.site_address("Supreme Cold") == "MIDC, Turbhe, Navi Mumbai"
    assert S.site_address("Warehouse A185").startswith("A-185, MIDC TTC")
    assert "A-824/1" in S.site_address("Eskimo")
    assert S.site_address("Savla Bond") == "", "no address was given for Savla Bond: print the name only"
    assert S.site_address(None) == ""


# ── site_aliases / scope_keys ──────────────────────────────────────────────

def test_site_aliases():
    assert S.site_aliases("D-39") == ["Savla D-39", "Savla D-39 Cold Storage", "D-39",
                                      "Savla D-39 Cold", "Old Savla", "Savla D-34"]
    assert S.site_aliases("supreme cold") == ["Supreme", "Supreme Cold Storage", "Supreme Cold"]
    assert S.site_aliases(" Store ") == ["Store"]
    assert S.site_aliases("") == [] and S.site_aliases(None) == []


def test_scope_keys_cover_every_spelling_of_each_site():
    keys = S.scope_keys(["D-39", "A185"])
    assert keys == ["SAVLAD39", "SAVLAD39COLDSTORAGE", "D39", "SAVLAD39COLD", "OLDSAVLA", "SAVLAD34",
                    "A185", "WAREHOUSEA185", "A185COLD"]
    assert "SAVLABOND" not in keys and "COLDSTORAGE" not in keys
    assert S.scope_keys(["Rishi"]) == ["RISHI", "RISHICOLDSTORAGE", "RISHICOLD", "RISHICOLDSTORAGEPVTLTD"]
    assert "RISHI" not in S.scope_keys(["Cold Storage"])


def test_scope_keys_edge_cases():
    assert S.scope_keys(None) == [] and S.scope_keys([]) == [] and S.scope_keys(["", None, " - "]) == []
    assert S.scope_keys(["Store"]) == ["STORE"], "an unknown grant keeps matching its own spelling"
    assert S.scope_keys("A185") == S.scope_keys(["A185"]), "a lone string is one site, not its letters"
    assert S.scope_keys(["D-39", "Savla D-39", "d39"]) == S.scope_keys(["D-39"]), "deduped"
    assert all(k == normalise_warehouse_code(k) for k in S.scope_keys(S.to_sites()))


def _norm_any(value: str, keys: list[str]) -> bool:
    """WAREHOUSE_NORM_ANY_SQL, evaluated in Python: both sides normalised."""
    return normalise_warehouse_code(value) in {normalise_warehouse_code(k) for k in keys}


def test_scope_keys_bound_into_the_norm_any_clause_match_every_stored_spelling():
    sql = WAREHOUSE_NORM_ANY_SQL("h.from_site", "$3")
    assert "unnest($3::text[])" in sql and "regexp_replace(upper(coalesce(h.from_site" in sql
    lock = S.scope_keys(["D-39"])
    for stored in ["Savla D-39", "savla d39", "D-39", "Savla D-39 Cold Storage", "Old Savla", "Savla D-34"]:
        assert _norm_any(stored, lock), stored
    for stored in ["Savla Bond", "Savla D-514", "Cold Storage", "Savla", "Rishi"]:
        assert not _norm_any(stored, lock), stored


# ── The lists ──────────────────────────────────────────────────────────────

def test_lists():
    assert S.warehouse_sites() == ["W202", "A185", "A101", "A68", "F53"]
    assert S.cold_sites() == ["Rishi", "Savla D-39", "Savla D-514", "Savla Bond", "Supreme", "Eskimo"]
    assert S.to_sites() == S.warehouse_sites() + S.cold_sites()
    assert S.grantable_codes() == ["W202", "A185", "A101", "A68", "F53", "Rishi", "D-39", "D-514",
                                   "Savla Bond", "Supreme", "Eskimo"]


def test_cold_storage_is_in_no_list():
    for listed in (S.warehouse_sites(), S.cold_sites(), S.to_sites(), S.grantable_codes()):
        assert not any(S.same_site(s, "Cold Storage") for s in listed)


def test_every_grantable_code_and_to_site_resolves():
    for code in S.grantable_codes():
        assert S.canonical_site(code) in S.to_sites(), code
    for site in S.to_sites():
        assert S.canonical_site(site) == site and S.site_kind(site) in {"warehouse", "cold"}


def test_the_lists_are_copies():
    S.to_sites().append("X")
    S.cold_sites().clear()
    assert "X" not in S.to_sites() and S.cold_sites()


def test_site_key_is_the_warehouse_scope_normaliser():
    assert S.site_key is normalise_warehouse_code
    assert S.site_key(" W-202 ") == "W202" and S.site_key(None) == "" and S.site_key("dev_int") == "DEVINT"


def test_a_key_claimed_twice_refuses_to_load(tmp_path):
    rows = json.loads(BACKEND_JSON.read_text(encoding="utf-8"))
    rows["sites"][1]["aliases"].append("W 202")          # A185 claims W202's key
    bad = tmp_path / "sites.json"
    bad.write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape("'W 202' is a spelling of both 'W202' and 'A185'")):
        S._load(bad)


# ── warehouse_scope rebuilt on same_site ───────────────────────────────────

HAS = [
    (["D-39"], "Savla D-39", True), (["D-39"], "Savla D39", True), (["A185"], "Warehouse A185", True),
    (["A185"], "A-185", True), (["Supreme"], "Supreme Cold", True), (["W-202"], "W202", True),
    (["Savla Bond"], "Savla Bond", True), (["Eskimo"], "ESKIMO", True), (["Store"], "store", True),
    (["W202", "D-514"], "New Savla", True),
    (["Rishi"], "Cold Storage", False), (["D-39"], "Savla Bond", False), (["Savla Bond"], "D-39", False),
    (["W202"], "A185", False), (["A185"], "", False), (["A185"], None, False), ([], "A185", False),
    (None, "A185", False), ([""], "", False),
]


@pytest.mark.parametrize("grants,code,want", HAS)
def test_user_has_warehouse(grants, code, want):
    assert user_has_warehouse(grants, code) is want


def test_user_has_any_warehouse():
    assert user_has_any_warehouse(["D-39"], ["X", "Savla D-39"])
    assert not user_has_any_warehouse(["A185"], ["W202", "W-202"])
    assert not user_has_any_warehouse(["A185"], [])


def test_production_spellings_keep_their_keys():
    """job_card_v2 / plan_v2 store 'W-202' / 'A-185' and grants say 'W202' / 'A185'."""
    for grant, stored in [("W202", "W-202"), ("A185", "A-185"), ("W-202", "W202"), ("A-185", "A185")]:
        assert user_has_warehouse([grant], stored)
        assert S.canonical_site(stored) == grant.replace("-", "")
