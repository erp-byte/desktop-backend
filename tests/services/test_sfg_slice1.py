"""SFG / Job-Card Slice-1 regression tests (DB-free seams).

The DB behaviour (343-row ingest, 8-digit BIGINT ids, re-type-in-place,
idempotency, concurrency) is exercised by the Docker integration harness; these
lock in the pure seams that don't need a connection:

  - normalise_key: NBSP/zero-width handling, idempotency, conservative mojibake
    repair (and NON-corruption of legitimate accented text)
  - the SFG_Catalog_Plug invariants the migration/ingest rely on (343 rows,
    unique codes, unique normalised names, uom is the non-numeric 'kg')
  - cost-gate parity: backend COST_BEARING_FIELDS == web cost-gate.ts set
  - strip_cost_fields strips the reserved SFG/WIP keys and descends into tuples

Runs under pytest (CI) and as a plain script (`python test_sfg_slice1.py`).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from app.core.helpers import normalise_key
from app.modules.production.services.response_filters import (
    COST_BEARING_FIELDS,
    strip_cost_fields,
)

_SERVER_ROOT = Path(__file__).resolve().parents[2]          # server_replica
_REPO_ROOT = _SERVER_ROOT.parent                            # CANDOR_ERP
_PLUG = (_SERVER_ROOT / "docs" / "flows"
         / "Candor_SFG_JobCard_Integration_2026-06-14" / "SFG_Catalog_Plug.csv")
_COST_GATE_TS = _REPO_ROOT / "web_replica" / "src" / "lib" / "cost-gate.ts"


# ── normalise_key ──────────────────────────────────────────────────────────

def test_normalise_key_strips_trailing_nbsp():
    assert normalise_key("SFG0123\xa0") == "SFG0123"


def test_normalise_key_collapses_whitespace_and_nbsp():
    assert normalise_key("Cafe  Galleries\xa0Sunflower  Seeds") == "Cafe Galleries Sunflower Seeds"


def test_normalise_key_none_and_empty():
    assert normalise_key(None) == ""
    assert normalise_key("   ") == ""


def test_normalise_key_idempotent_over_plug():
    for name in (r["particulars"] for r in _plug_rows()):
        once = normalise_key(name)
        assert normalise_key(once) == once


def test_normalise_key_repairs_mojibake():
    # 'donâ€™t' is the UTF-8-as-cp1252 mojibake of "don’t"
    assert normalise_key("donâ€™t") == "don’t"


def test_normalise_key_preserves_legitimate_accents():
    # Valid accented text must NOT be mangled by the mojibake repair.
    assert normalise_key("Crème Brûlée") == "Crème Brûlée"


def test_normalise_key_deletes_zero_width():
    # ZWSP is removed (not turned into a space).
    assert normalise_key("A​B") == "AB"


# ── SFG_Catalog_Plug invariants ────────────────────────────────────────────

def _plug_rows():
    with _PLUG.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_plug_has_343_rows():
    assert len(_plug_rows()) == 343


def test_plug_codes_unique_and_well_formed():
    codes = [r["sfg_code"] for r in _plug_rows()]
    assert len(set(codes)) == len(codes) == 343
    assert all(re.fullmatch(r"SFG[0-9]{4}", c) for c in codes)


def test_plug_normalised_names_unique():
    names = [normalise_key(r["particulars"]) for r in _plug_rows()]
    assert len(set(names)) == 343


def test_plug_uom_is_non_numeric_kg():
    # Every plug uom is 'kg' — proving it must be stored NULL (all_sku.uom is NUMERIC).
    assert {r["uom"] for r in _plug_rows()} == {"kg"}


def test_plug_all_typed_sfg_wip():
    rows = _plug_rows()
    assert {r["item_type"] for r in rows} == {"sfg"}
    assert {r["sale_group"] for r in rows} == {"wip"}


# ── cost-gate parity + stripping ───────────────────────────────────────────

_SFG_WIP_COST_KEYS = {
    "sfg_unit_cost", "wip_unit_cost", "sfg_cost_per_kg", "wip_cost_per_kg",
    "sfg_valuation", "wip_valuation", "wip_stock_value", "wip_batch_value",
}


def test_reserved_sfg_wip_cost_keys_present():
    assert _SFG_WIP_COST_KEYS <= COST_BEARING_FIELDS


def test_cost_gate_backend_web_parity():
    if not _COST_GATE_TS.exists():               # backend-only checkout: skip
        return
    txt = _COST_GATE_TS.read_text(encoding="utf-8")
    m = re.search(r"COST_BEARING_FIELDS:\s*ReadonlySet<string>\s*=\s*new Set\(\[(.*?)\]\)", txt, re.S)
    web = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert web == set(COST_BEARING_FIELDS), web ^ set(COST_BEARING_FIELDS)


def test_strip_cost_fields_strips_sfg_for_deny_role():
    payload = {"sku": "SFG0001", "wip_valuation": 123.0, "qty_kg": 5}
    out = strip_cost_fields(payload, "team_leader")
    assert "wip_valuation" not in out and out["qty_kg"] == 5


def test_strip_cost_fields_descends_into_tuple():
    payload = {"rows": ({"sfg_unit_cost": 9, "name": "x"},)}
    out = strip_cost_fields(payload, "viewer")
    assert "sfg_unit_cost" not in out["rows"][0] and out["rows"][0]["name"] == "x"


def test_strip_cost_fields_allow_role_untouched():
    payload = {"wip_valuation": 1}
    assert strip_cost_fields(payload, "admin", is_admin=True) == payload


if __name__ == "__main__":  # plain-script runner (no pytest needed)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} SFG Slice-1 unit tests passed")
