"""The SKU-master lookup must be reachable by roles that are NOT sales-order users.

GET /api/v1/so/sku-lookup backs the shared ArticlePicker (Search + Browse tabs)
on the NPD dev job-card recipe, the NPD draft-BOM section and the sample
requisition form — but it gated on the bare `so` view permission, which only
admin / so_creator / viewer hold. npd_team (sample-module grants only) therefore
got HTTP 403 on every keystroke, and the picker renders a rejected lookup as an
empty list, so the operator just saw "No matching articles."

Migration 090 splits the lookup onto its own `so/sku_lookup` sub-module row and
grants it to the sample-flow roles. check_permission falls back sub -> NULL, so
the existing bare-`so:view` holders keep passing unchanged.

No DB: the migration and the router source are parsed, and check_permission is
exercised against a stub connection.

Run:  PYTHONPATH=. python -m pytest tests/services/test_sku_lookup_permission.py
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app.modules.auth.services.permission_service import check_permission

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "app" / "db" / "090_sku_lookup_permission.sql"
SO_ROUTER = (ROOT / "app" / "modules" / "so" / "router.py").read_text(encoding="utf-8")

# Roles that reach an ArticlePicker / SKU-cascade screen through the sample+NPD
# module but hold no `so` grant of their own.
SAMPLE_FLOW_ROLES = ("npd_team", "business_head", "sales", "planner")


# ── the gate on the endpoint ─────────────────────────────────────────────────

def _dependency_for(route: str) -> str:
    """The require_permission(...) call guarding a given @router.get path.

    Blocks are split on decorators anchored at column 0 so a NOTE comment that
    quotes a route (`must stay ABOVE @router.get("/{so_id}")`) can't be mistaken
    for the route itself — it was, and the test passed against the wrong handler.
    """
    blocks = re.split(r"^(?=@router\.)", SO_ROUTER, flags=re.M)
    hits = [b for b in blocks
            if re.match(rf'@router\.get\(\s*"{re.escape(route)}"\s*[,)]', b)]
    assert len(hits) == 1, f'expected 1 @router.get("{route}"), found {len(hits)}'
    # Signature only — stop at the handler's docstring/body so a nested call in
    # the implementation can't be picked up instead.
    sig = hits[0].split("):", 1)[0]
    dep = re.search(r"require_permission\(([^)]*)\)", sig)
    assert dep, f"{route} has no require_permission dependency"
    return dep.group(1)


def _lookup_routes() -> list[str]:
    """Every @router.get path under /sku-lookup, discovered rather than listed:
    a sibling lookup added later (e.g. /sku-lookup/bulk, which lives on an
    unmerged branch) is then covered the day it lands instead of quietly
    shipping with the bare `so:view` gate this migration exists to remove."""
    routes = re.findall(r'^@router\.get\(\s*"(/sku-lookup[^"]*)"', SO_ROUTER, re.M)
    assert "/sku-lookup" in routes, "the cascade endpoint itself is missing"
    return routes


def test_every_lookup_route_is_gated_on_the_sku_lookup_sub_module():
    """Not the bare `so` view permission — that is the sales-order read surface
    (GET /so/{so_id}, the SO list, GST recon), which the NPD team must not get
    just to type an ingredient name."""
    for route in _lookup_routes():
        dep = _dependency_for(route)
        assert 'sub_module="sku_lookup"' in dep, (
            f"{route} gates on {dep!r}; sample-flow roles will 403")


def test_the_sales_order_read_surface_is_still_gated_on_bare_so_view():
    """Guard against over-correcting: only the lookups move sub-module."""
    assert 'sub_module' not in _dependency_for("/{so_id}")


# ── the migration ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sql() -> str:
    assert MIGRATION.exists(), f"{MIGRATION.name} missing"
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_is_registered_in_the_runner():
    runner = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    assert f'"{MIGRATION.name}"' in runner, "migration exists but would never run"


def test_migration_adds_the_catalog_row(sql):
    assert re.search(
        r"INSERT INTO auth_permission\b.*?'so'\s*,\s*'sku_lookup'\s*,\s*NULL\s*,\s*'view'",
        sql, re.S | re.I), "no (so, sku_lookup, NULL, view) catalog row"


def test_catalog_insert_is_idempotent_despite_the_null_sub_sub_module(sql):
    """auth_permission's UNIQUE is defeated by NULL sub_sub_module (see 084), so
    ON CONFLICT alone would not make a re-run safe — the insert must guard with
    NOT EXISTS."""
    ins = re.search(r"INSERT INTO auth_permission\b.*?;", sql, re.S | re.I)
    assert ins, "no auth_permission insert"
    assert re.search(r"NOT EXISTS", ins.group(0), re.I), (
        "re-running would insert a duplicate (so, sku_lookup, NULL, view) row")


def _granted_roles(sql: str) -> set[str]:
    """Role names named by any `role_name = 'x'` / `role_name IN ('x', 'y')`
    predicate inside an auth_role_permission insert."""
    roles: set[str] = set()
    for grant in re.findall(r"INSERT INTO auth_role_permission\b.*?;", sql, re.S | re.I):
        for pred in re.findall(
                r"role_name\s*(?:=\s*'([^']+)'|IN\s*\(([^)]*)\))", grant, re.I):
            eq, in_list = pred
            if eq:
                roles.add(eq)
            roles.update(re.findall(r"'([^']+)'", in_list))
    return roles


@pytest.mark.parametrize("role", SAMPLE_FLOW_ROLES)
def test_migration_grants_the_role(sql, role):
    assert role in _granted_roles(sql), f"{role} never granted"


def test_migration_grants_no_unexpected_role(sql):
    """admin is seeded for catalog parity (it bypasses the check anyway);
    anything else showing up here is scope creep."""
    assert _granted_roles(sql) - {"admin"} == set(SAMPLE_FLOW_ROLES)


def test_migration_grants_nothing_but_the_lookup(sql):
    """A grant that matched on module='so' alone would hand the sample-flow
    roles the whole sales-order read surface."""
    for grant in re.findall(r"INSERT INTO auth_role_permission\b.*?;", sql, re.S | re.I):
        assert "'sku_lookup'" in grant, f"grant is not scoped to sku_lookup:\n{grant}"


def test_grants_are_idempotent(sql):
    for grant in re.findall(r"INSERT INTO auth_role_permission\b.*?;", sql, re.S | re.I):
        assert re.search(r"ON CONFLICT DO NOTHING", grant, re.I), "grant is not re-runnable"


# ── the resulting permission decisions ───────────────────────────────────────

class _StubConn:
    """Answers check_permission's lookup out of an in-memory grant table of
    (module, sub_module, sub_sub_module, action) tuples."""

    def __init__(self, *grants):
        self.grants = set(grants)

    async def fetch(self, _query, role_ids, mod, sub, subsub, act):  # noqa: ARG002
        hit = (mod, sub, subsub, act) in self.grants
        return [{"allowed_entities": None, "allowed_warehouses": None,
                 "allowed_floors": None}] if hit else []


def _allows(conn, sub_module, action="view"):
    return asyncio.run(check_permission(
        conn, [7], False, "so", sub_module=sub_module, action=action))


SKU_LOOKUP_ONLY = ("so", "sku_lookup", None, "view")
BARE_SO_VIEW = ("so", None, None, "view")


def test_sample_flow_role_can_reach_the_lookup():
    assert _allows(_StubConn(SKU_LOOKUP_ONLY), "sku_lookup") is True


def test_sample_flow_role_still_cannot_read_sales_orders():
    """The whole point of the sub-module: the lookup grant must NOT satisfy the
    bare so:view gate on GET /so/{so_id}."""
    assert _allows(_StubConn(SKU_LOOKUP_ONLY), None) is False


def test_existing_so_view_holders_keep_the_lookup():
    """so_creator / viewer hold only the bare row; check_permission's
    sub -> NULL fallback must still let them through — no regression."""
    assert _allows(_StubConn(BARE_SO_VIEW), "sku_lookup") is True


def test_a_role_with_neither_grant_is_still_refused():
    assert _allows(_StubConn(), "sku_lookup") is False


def test_admin_bypasses_regardless():
    assert asyncio.run(check_permission(
        _StubConn(), [7], True, "so", sub_module="sku_lookup")) is True
