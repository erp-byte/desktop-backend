"""B13 cost-metric access gate.

Cross-cutting response filter that strips currency-denominated fields
out of any JC / accounting / dashboard payload when the caller's role
is in the deny list. See memory note [[cost-metric-access-gate]]:

  Deny list (no cost visibility):
    team_leader, qc_inspector, floor_manager, viewer

  Allow list (cost visible):
    admin, planner, purchase_manager, inventory_manager,
    (future) commercial_manager, cost_controller

  Default-deny:
    Any role not in the allow list is treated as deny. Adding a role
    to the allow list is an explicit opt-in.

The gate enforces at the API serialisation layer so a direct API hit
can't reach cost fields via a frontend bypass.

Usage at every read endpoint that may surface cost columns:

    from app.modules.production.services.response_filters import strip_cost_fields
    return strip_cost_fields(payload, user.role_name, is_admin=user.is_admin)

For mixed-content endpoints (accounting GET, JC detail, JC list, PDF):
    -> strip the cost fields, return the rest.
For cost-pure endpoints (future commercial dashboards):
    -> raise HTTPException(403) for deny-listed callers.
"""
from __future__ import annotations

import copy
from typing import Any


# Canonical role lists from the memory note.
COST_FIELDS_DENY_ROLES = frozenset({
    "team_leader", "qc_inspector", "floor_manager", "viewer",
})

COST_FIELDS_ALLOW_ROLES = frozenset({
    "admin", "planner", "purchase_manager", "inventory_manager",
    "business_head",                           # sample module — management role sees cost
    "commercial_manager", "cost_controller",  # future
})
# NOTE: npd_team is intentionally NOT here — default-deny (sample open decision).

# Every cost-bearing field name across the v2 surface. Append here as
# new cost columns / JSONB keys land - the central list keeps one
# allowlist owner instead of N scattered checks.
#
# Grouped logically so future additions land in the right bucket and
# code review can see WHY each field is gated. The memory-note
# enumeration ("unit cost ₹/kg, variance cost impact ₹, batch total
# cost, margin %, cost basis labels, inventory ledger valuations,
# anything denominated in currency") drives the grouping.
COST_BEARING_FIELDS = frozenset({
    # ── Variance / consumption costing ────────────────────────────────
    # job_card_consumption_variance_v2 (migration 028, future commercial)
    "unit_cost_at_consumption", "variance_cost_impact", "cost_basis",

    # ── Currency-denominated INR fields surfaced by JC v2 detail ──────
    # SO/PO and accounting payloads round-trip these straight from the
    # ledger; gating at the serialiser is the only safe layer.
    "rate_inr", "amount_inr", "total_amount_inr",

    # ── Generic rate / price columns ──────────────────────────────────
    # Any of these can appear on BOM, SO line, PO line, or invoice rows
    # that get embedded into a JC / fulfillment / dashboard response.
    "rate", "price", "unit_price", "selling_price",
    "mrp", "list_price",

    # ── Inventory ledger valuations ───────────────────────────────────
    # WAC / FIFO / standard-cost snapshots on inventory and ledger rows;
    # explicitly named in the memory note as gated.
    "landed_cost", "ledger_value", "stock_value", "valuation",
    "wac_cost", "fifo_cost", "standard_cost",

    # ── Aggregate amount fields ───────────────────────────────────────
    # Net / gross / generic 'amount' columns on invoices, POs, and
    # accounting summaries. The dimensionless qty columns stay visible;
    # only the currency-denominated aggregates are stripped.
    "amount", "gross_amount", "net_amount",
    "batch_total_cost", "total_cost",

    # ── Per-unit cost ratios ──────────────────────────────────────────
    # Derived from unit cost — memory note: "directly derived from unit
    # cost" is the line, so these are gated even though they look like
    # an operational metric at first glance.
    "unit_cost", "cost_per_unit", "cost_per_pack", "cost_per_kg",

    # ── Material / labour / overhead breakdown ────────────────────────
    # Cost-of-goods breakdown that often rides along with JC accounting
    # summaries (variance phase 2+).
    "material_cost", "labour_cost", "overhead_cost",

    # ── Margin (percentage but derived from cost) ─────────────────────
    # Memory note explicitly lists margin % as gated even though the
    # number itself is dimensionless.
    "margin_pct",

    # ── Tax & charges (currency in INR) ───────────────────────────────
    # GST splits + APMC cess + packing/freight/processing surcharges
    # ride along on every SO/PO line and JC accounting summary. Added
    # alongside the SO router gating wiring (C12) so a deny-listed role
    # can't reconstruct the line total from the per-component amounts.
    "igst_amount", "sgst_amount", "cgst_amount",
    "apmc_amount", "packing_amount", "freight_amount", "processing_amount",

    # ── SFG / WIP valuation (RESERVED — Slice 1, enforced in Slice 5) ──────
    # Reserved up front so the gate exists before any cost value flows on the
    # SFG/WIP surface (WIP inventory materialisation + the sfg-inventory
    # picker). Mirror EXACTLY in web_replica cost-gate.ts COST_BEARING_FIELDS.
    "sfg_unit_cost", "wip_unit_cost",
    "sfg_cost_per_kg", "wip_cost_per_kg",
    "sfg_valuation", "wip_valuation",
    "wip_stock_value", "wip_batch_value",
})


def is_cost_field(field_name: str) -> bool:
    """Boundary check used by the filter helpers."""
    return field_name in COST_BEARING_FIELDS


def sees_cost(role_name: str | None, *, is_admin: bool = False) -> bool:
    """True if the caller is allowed to see cost fields.

    Default-deny: a role we don't recognise is treated as deny. Admin
    bypasses the allow list (they see everything).
    """
    if is_admin:
        return True
    if not role_name:
        return False
    return role_name in COST_FIELDS_ALLOW_ROLES


def strip_cost_fields(payload: Any, role_name: str | None,
                       *, is_admin: bool = False) -> Any:
    """Walk a nested dict / list structure and drop every cost-bearing
    key from any nested dict, when the caller is not allowed to see
    cost.

    Returns a NEW object — input is never mutated. We deep-copy on entry
    (only when stripping is needed) so callers can keep using their
    payload without action-at-a-distance surprises. When the caller is
    allowed to see cost, the original payload is returned as-is (no
    copy, no traversal).

    Lists are walked recursively. Non-dict, non-list values are returned
    as-is (we never strip an inner string by name).
    """
    if sees_cost(role_name, is_admin=is_admin):
        return payload
    return _strip_recursive(copy.deepcopy(payload))


def _strip_recursive(node: Any) -> Any:
    """Walk a (deep-copied) tree and delete cost-bearing keys in place.

    Safe to mutate here because strip_cost_fields has already deep-copied
    the input — the caller's object is untouched. Keeping the in-place
    mutation on the local copy avoids allocating a second copy of every
    nested container.
    """
    if isinstance(node, dict):
        for key in list(node.keys()):
            if is_cost_field(key):
                del node[key]
            else:
                node[key] = _strip_recursive(node[key])
        return node
    if isinstance(node, list):
        for i in range(len(node)):
            node[i] = _strip_recursive(node[i])
        return node
    # Tuples (incl. asyncpg Record, which is tuple-like) are immutable: rebuild a
    # scrubbed copy so a cost dict nested inside a tuple isn't passed through raw.
    # NOTE: a tuple has no field NAMES, so a top-level Record's own cost columns
    # can't be stripped by name — strip at the dict boundary (.model_dump() /
    # dict(record)) as the existing call sites already do.
    if isinstance(node, tuple):
        return tuple(_strip_recursive(x) for x in node)
    return node


def assert_can_see_cost(role_name: str | None, *, is_admin: bool = False) -> None:
    """Raise HTTPException 403 if the caller is in the deny list.
    For cost-pure endpoints where the entire payload is sensitive (no
    field-strip fallback). Imported lazily to avoid forcing fastapi
    into modules that just want is_cost_field / sees_cost.
    """
    if sees_cost(role_name, is_admin=is_admin):
        return
    from fastapi import HTTPException
    raise HTTPException(
        status_code=403,
        detail={
            "error": "cost_metric_access_denied",
            "role":  role_name,
            "message": (
                "This endpoint surfaces cost metrics which are restricted "
                "to admin, planner, purchase_manager, inventory_manager, "
                "or future commercial roles."
            ),
        },
    )
