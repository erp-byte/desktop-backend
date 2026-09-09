"""Every Stock Take endpoint must require the `stock_take` permission.

This is a wiring test, not a behaviour test. The endpoints used to run on
get_current_user, which authenticates but authorises nothing -- any signed-in
user could read counted stock and post adjustments. Nothing about that looked
wrong at a call site: `Depends(get_current_user)` is exactly what a gated route
also looks like, one identifier different.

So the check is mechanical: walk the mounted routes and read the dependency each
one actually resolved, which is what FastAPI will run at request time rather than
what the source appears to say.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.modules.stock_take.router import router

#: path -> {method: action}. Update deliberately: adding a row here is a claim
#: about what a new endpoint is allowed to do.
EXPECTED = {
    "/api/v1/stock-take/latest-stock":        {"GET": "view"},
    "/api/v1/stock-take/filter-options":      {"GET": "view"},
    "/api/v1/stock-take/scope":               {"GET": "view"},
    "/api/v1/stock-take/transactions":        {"GET": "view", "POST": "create"},
    "/api/v1/stock-take/transactions/export": {"GET": "export"},
    "/api/v1/stock-take/balance":             {"GET": "view"},
    "/api/v1/stock-take/entries/export":      {"GET": "export"},
}


def _permission_of(route: APIRoute) -> tuple[str, str] | None:
    """(module, action) enforced by this route, read off the closure.

    require_permission returns a closure over (module, sub_module,
    sub_sub_module, action); co_freevars names them, so the pairing survives an
    argument being reordered.
    """
    for dep in route.dependant.dependencies:
        fn = dep.call
        free = getattr(fn, "__code__", None)
        cells = getattr(fn, "__closure__", None)
        if not free or not cells:
            continue
        names = dict(zip(free.co_freevars, (c.cell_contents for c in cells)))
        if "module" in names and "action" in names:
            return names["module"], names["action"]
    return None


ROUTES = [r for r in router.routes if isinstance(r, APIRoute)]


def test_every_route_is_accounted_for():
    """A new endpoint must be added to EXPECTED, not silently inherit nothing."""
    seen = {(r.path, m) for r in ROUTES for m in r.methods if m != "HEAD"}
    want = {(p, m) for p, ms in EXPECTED.items() for m in ms}
    assert seen == want, (
        f"routes not in EXPECTED: {sorted(seen - want)}\n"
        f"EXPECTED entries with no route: {sorted(want - seen)}"
    )


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.path)
def test_route_requires_the_stock_take_permission(route):
    got = _permission_of(route)
    assert got is not None, (
        f"{route.path} has no require_permission dependency -- it is open to any "
        f"authenticated user. get_current_user is authentication, not authorisation."
    )
    module, action = got
    assert module == "stock_take", f"{route.path} gates on {module!r}"
    for method in route.methods:
        if method == "HEAD":
            continue
        assert action == EXPECTED[route.path][method], (
            f"{method} {route.path} enforces {action!r}, expected "
            f"{EXPECTED[route.path][method]!r}"
        )


def test_reads_and_writes_are_not_the_same_permission():
    """Posting an adjustment must not be reachable with read-only access."""
    actions = {a for r in ROUTES if (p := _permission_of(r)) for a in (p[1],)}
    assert actions == {"view", "create", "export"}, actions


def test_no_route_still_uses_bare_authentication():
    from app.modules.auth.middleware import get_current_user

    for r in ROUTES:
        calls = {d.call for d in r.dependant.dependencies}
        assert get_current_user not in calls, f"{r.path} still uses get_current_user"
