"""B13 cost-metric access gate — unit tests.

Covers the allow / deny matrix from the memory note and the safety
guarantees of strip_cost_fields:

  - admin bypasses the allow list entirely
  - allow-listed roles (planner) see cost fields
  - deny-listed roles (team_leader) get cost fields stripped
  - unknown roles default to deny
  - None role defaults to deny
  - nested dict-in-list payloads strip recursively
  - payloads with no cost fields are returned unchanged
  - input payload is never mutated (M1)
"""
from __future__ import annotations

import copy

import pytest

from app.modules.production.services.response_filters import (
    COST_BEARING_FIELDS,
    sees_cost,
    strip_cost_fields,
)


# ---------------------------------------------------------------------------
# sees_cost matrix
# ---------------------------------------------------------------------------

def test_admin_bypass():
    """is_admin=True wins regardless of role_name."""
    assert sees_cost(None, is_admin=True) is True
    assert sees_cost("team_leader", is_admin=True) is True
    assert sees_cost("unknown_future_role", is_admin=True) is True


def test_planner_sees_cost():
    """Allow-listed roles return True."""
    assert sees_cost("planner") is True
    assert sees_cost("purchase_manager") is True
    assert sees_cost("inventory_manager") is True
    # Future roles already in the allow list.
    assert sees_cost("commercial_manager") is True
    assert sees_cost("cost_controller") is True


def test_team_leader_denied():
    """team_leader is the canonical deny-list role."""
    assert sees_cost("team_leader") is False


def test_unknown_role_denied():
    """Default-deny: any role not in the allow list is denied."""
    assert sees_cost("some_random_role") is False
    assert sees_cost("") is False


def test_none_role_denied():
    """None role_name (anonymous / missing user attr) defaults to deny."""
    assert sees_cost(None) is False


# ---------------------------------------------------------------------------
# strip_cost_fields — top-level dict
# ---------------------------------------------------------------------------

def test_team_leader_stripped_top_level():
    """Cost-bearing keys are removed for a deny-listed role."""
    payload = {
        "rate_inr": 100,
        "amount_inr": 500,
        "qty_kg": 5,
        "process_name": "roasting",
    }
    out = strip_cost_fields(payload, "team_leader")
    assert "rate_inr" not in out
    assert "amount_inr" not in out
    # Operational metrics stay visible.
    assert out["qty_kg"] == 5
    assert out["process_name"] == "roasting"


def test_unknown_role_stripped():
    """Default-deny path strips the same as the canonical deny list."""
    payload = {"rate_inr": 100, "qty_kg": 5}
    out = strip_cost_fields(payload, "some_random_role")
    assert "rate_inr" not in out
    assert out["qty_kg"] == 5


def test_none_role_stripped():
    """A None role triggers the deny path."""
    payload = {"rate_inr": 100, "qty_kg": 5}
    out = strip_cost_fields(payload, None)
    assert "rate_inr" not in out
    assert out["qty_kg"] == 5


def test_planner_sees_cost_untouched():
    """Allow-listed callers get the same object back, untouched."""
    payload = {"rate_inr": 100, "qty_kg": 5}
    out = strip_cost_fields(payload, "planner")
    # For the allow path we explicitly return the same reference (no copy).
    assert out is payload
    assert out["rate_inr"] == 100


def test_admin_sees_cost_untouched():
    """is_admin=True short-circuits even when role_name is deny-listed."""
    payload = {"rate_inr": 100, "qty_kg": 5}
    out = strip_cost_fields(payload, "team_leader", is_admin=True)
    assert out is payload
    assert out["rate_inr"] == 100


# ---------------------------------------------------------------------------
# strip_cost_fields — nested structures
# ---------------------------------------------------------------------------

def test_nested_dict_in_list_stripped():
    """A list of dicts (typical JC list response) is stripped recursively."""
    payload = [
        {"job_card_id": 1, "rate_inr": 100, "qty_kg": 5},
        {"job_card_id": 2, "amount_inr": 250, "qty_kg": 7},
    ]
    out = strip_cost_fields(payload, "team_leader")
    assert len(out) == 2
    assert out[0] == {"job_card_id": 1, "qty_kg": 5}
    assert out[1] == {"job_card_id": 2, "qty_kg": 7}


def test_deeply_nested_stripped():
    """Cost keys deep in a tree are stripped at every level."""
    payload = {
        "header": {
            "job_card_id": 1,
            "rate_inr": 100,                   # gated
        },
        "lines": [
            {"sku": "A", "mrp": 200, "qty": 1},  # mrp gated
            {"sku": "B", "list_price": 300, "qty": 2},  # list_price gated
        ],
        "totals": {
            "amount_inr": 500,                 # gated
            "qty_total": 3,
        },
    }
    out = strip_cost_fields(payload, "team_leader")
    assert "rate_inr" not in out["header"]
    assert out["header"]["job_card_id"] == 1
    assert out["lines"][0] == {"sku": "A", "qty": 1}
    assert out["lines"][1] == {"sku": "B", "qty": 2}
    assert "amount_inr" not in out["totals"]
    assert out["totals"]["qty_total"] == 3


def test_payload_without_cost_fields_untouched():
    """Payloads with no cost-bearing keys come back equal to the input."""
    payload = {
        "job_card_id": 1,
        "process_name": "roasting",
        "qty_kg": 5,
        "team_members": ["alice", "bob"],
    }
    original = copy.deepcopy(payload)
    out = strip_cost_fields(payload, "team_leader")
    assert out == original


# ---------------------------------------------------------------------------
# M1 — input must never be mutated
# ---------------------------------------------------------------------------

def test_input_not_mutated_top_level():
    """strip_cost_fields must deep-copy on entry — input stays intact."""
    payload = {"rate_inr": 100, "qty_kg": 5}
    original = copy.deepcopy(payload)
    _ = strip_cost_fields(payload, "team_leader")
    assert payload == original


def test_input_not_mutated_nested():
    """Nested containers must also survive untouched."""
    payload = {
        "lines": [
            {"sku": "A", "rate_inr": 100, "qty": 1},
        ],
    }
    original = copy.deepcopy(payload)
    _ = strip_cost_fields(payload, "team_leader")
    assert payload == original


# ---------------------------------------------------------------------------
# C1 — sanity check on the expanded COST_BEARING_FIELDS frozenset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "rate_inr", "amount_inr", "total_amount_inr",
    "rate", "price", "unit_price", "selling_price", "mrp", "list_price",
    "landed_cost", "ledger_value", "stock_value", "valuation",
    "amount", "gross_amount", "net_amount",
    "cost_per_unit", "cost_per_pack", "cost_per_kg",
    "unit_cost_at_consumption", "variance_cost_impact", "cost_basis",
    "margin_pct",
])
def test_cost_field_in_allowlist(field):
    """Each currency / cost field promised by the memory note is gated."""
    assert field in COST_BEARING_FIELDS
