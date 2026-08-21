"""Godown canonicalisation for the Inventory Ledger.

Raw warehouse values are inconsistent across the legacy inward tables; grouping
on the raw column fragments one physical godown into many ledger rows.

Run:  PYTHONPATH=. python -m pytest tests/services/test_ledger_godown_alias.py -v
"""
from __future__ import annotations

import pytest

from app.modules.ledger.services.godown_alias import (
    AMBIGUOUS_ALIASES,
    ledger_godown,
)


@pytest.mark.parametrize("raw", [
    "savla d-39", "Savla D39", "  SAVLA D-39  ", "d39", "d-39",
    "old savla", "old_savla", "savla-d39", "savla-d-39",
    "savla d-39 cold", "savla d39 cold",
])
def test_d39_aliases_collapse(raw):
    assert ledger_godown(raw) == "Savla D-39"


@pytest.mark.parametrize("raw", [
    "savla d-514", "savla d514", "d514", "d-514", "new savla", "new_savla",
    "savla-d514", "savla-d-514", "savla d-514 cold", "savla d514 cold",
])
def test_d514_aliases_collapse(raw):
    assert ledger_godown(raw) == "Savla D-514"


@pytest.mark.parametrize("raw", ["savla bond", "SAVLA BOND", "savla_bond"])
def test_savla_bond_is_its_own_godown(raw):
    """Both legacy copies fold this into D-39. The ledger keeps it separate."""
    assert ledger_godown(raw) == "Savla Bond"


@pytest.mark.parametrize("raw,expected", [
    ("a185", "A185"), ("warehouse a185", "A185"),
    ("a-185", "A185"), ("A-185 Cold", "A185"),
    ("w202", "W202"), ("a101", "A101"), ("a68", "A68"), ("f53", "F53"),
    ("dev int", "Dev Int"), ("dev_int", "Dev Int"),
    ("rishi cold storage", "Rishi"), ("supreme cold", "Supreme"),
    ("eskimo", "Eskimo"),
])
def test_remaining_canonical_warehouses(raw, expected):
    assert ledger_godown(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_warehouse_becomes_unassigned(raw):
    """NULL warehouse is a live path: neither v2 INSERT path writes the column."""
    assert ledger_godown(raw) == "Unassigned"


def test_unknown_value_passes_through_title_cased():
    """Never drop a godown — an unmapped one must still show up in totals."""
    assert ledger_godown("some new shed") == "Some New Shed"


def test_underscore_normalisation_is_applied():
    """Legacy matching does strip().lower().replace('_',' '); dropping it breaks
    new_savla / savla_bond / dev_int."""
    assert ledger_godown("new_savla") == "Savla D-514"


def test_bare_savla_is_flagged_ambiguous():
    """Inherited from inward_tools.py and absent from canonicalize.py. With
    Savla Bond split out this is an assumption, so it must be logged."""
    assert ledger_godown("savla") == "Savla D-39"
    assert "savla" in AMBIGUOUS_ALIASES
