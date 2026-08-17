"""The SKU-master lookup must stay reachable by every authenticated user.

GET /api/v1/so/sku-lookup backs the shared ArticlePicker (Search + Browse tabs)
used by the NPD dev job-card recipe, the NPD draft-BOM section, the sample
requisition form, Material In's walk-in intimation modal, customer returns and
SO creation. all_sku is reference data — article name / group / uom / gst — not
sales-order data.

Because the endpoint lives under /so it inherited the `so` module gate, which
only admin / so_creator / viewer hold. It therefore 403'd for npd_team,
business_head, sales, planner, purchase_manager and store_head, and the picker
renders a rejected lookup as an empty list — so the operator saw "No matching
articles." and reported it as a search that finds nothing.

Scoping it to a narrower permission just moves the problem: the next screen to
reuse the picker breaks a different role. So the lookups are authentication-only
and these tests pin that, because the failure mode is silent and the fix keeps
getting re-litigated.

No DB — the router source is parsed.

Run:  PYTHONPATH=. python -m pytest tests/services/test_sku_lookup_permission.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
SO_ROUTER = (ROOT / "app" / "modules" / "so" / "router.py").read_text(encoding="utf-8")


def _handler_signature(route: str) -> str:
    """The signature of the handler for a given @router.get path.

    Blocks are split on decorators anchored at column 0 so a NOTE comment that
    quotes a route (`must stay ABOVE @router.get("/{so_id}")`) can't be mistaken
    for the route itself — it was, and the test passed against the wrong handler.
    """
    blocks = re.split(r"^(?=@router\.)", SO_ROUTER, flags=re.M)
    hits = [b for b in blocks
            if re.match(rf'@router\.get\(\s*"{re.escape(route)}"\s*[,)]', b)]
    assert len(hits) == 1, f'expected 1 @router.get("{route}"), found {len(hits)}'
    # Signature only — stop at the body so a call inside the handler can't be
    # picked up instead of the dependency.
    return hits[0].split("):", 1)[0]


def _lookup_routes() -> list[str]:
    """Every @router.get path under /sku-lookup, discovered rather than listed,
    so a sibling lookup added later is covered the day it lands."""
    routes = re.findall(r'^@router\.get\(\s*"(/sku-lookup[^"]*)"', SO_ROUTER, re.M)
    assert "/sku-lookup" in routes, "the cascade endpoint itself is missing"
    return routes


def test_lookups_are_not_permission_gated():
    """The regression this file exists for. A require_permission dependency on
    these routes silently empties the ArticlePicker for every role that doesn't
    hold it — which is most of them."""
    for route in _lookup_routes():
        sig = _handler_signature(route)
        assert "require_permission" not in sig, (
            f"{route} is permission-gated again; every role without that grant "
            f"will see an empty article dropdown with no error")


def test_lookups_still_require_a_logged_in_user():
    """Un-gated is not un-authenticated — the article master is internal."""
    for route in _lookup_routes():
        assert "get_current_user" in _handler_signature(route), (
            f"{route} has no auth dependency at all")


def test_the_sales_order_surface_is_still_gated():
    """Guard against over-correcting: only the lookups opened up. The SO views
    carry real customer order data and keep the `so` gate."""
    for route in ("/{so_id}", "/view", "/export"):
        sig = _handler_signature(route)
        assert 'require_permission("so"' in sig, f"{route} lost its `so` gate"


def test_no_sku_lookup_permission_rows_are_left_behind():
    """An earlier fix introduced a `so/sku_lookup` permission + migration 090.
    Dropping the gate made both dead; a stale grant row would imply the endpoint
    is still gated and mislead the next reader of the RBAC catalog."""
    db = ROOT / "app" / "db"
    assert not (db / "090_sku_lookup_permission.sql").exists(), \
        "migration 090 is obsolete — the lookup is no longer permission-gated"
    runner = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    assert "090_sku_lookup_permission" not in runner, \
        "the runner still applies the obsolete migration"
    stray = [p.name for p in db.rglob("*.sql")
             if "sku_lookup" in p.read_text(encoding="utf-8", errors="ignore")]
    assert not stray, f"sku_lookup permission rows still seeded by: {stray}"
