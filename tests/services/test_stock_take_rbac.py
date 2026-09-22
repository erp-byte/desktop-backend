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
    "/api/v1/stock-take/latest-stock/export": {"GET": "export"},
    "/api/v1/stock-take/filter-options":      {"GET": "view"},
    "/api/v1/stock-take/scope":               {"GET": "view"},
    "/api/v1/stock-take/transactions":        {"GET": "view", "POST": "create"},
    "/api/v1/stock-take/transactions/export": {"GET": "export"},
    # Sign-off. A separate action ON PURPOSE: the stock_take role holds create
    # but NOT verify, so nobody can both post an adjustment and approve it.
    "/api/v1/stock-take/adjustments/verify": {"POST": "verify"},
    # The per-posting half of the sign-off. Same gate as the line-level one, and
    # deliberately so: the two reconcile into each other (verifying the last
    # posting signs its line off, verifying a line signs off its postings), so a
    # weaker gate here would be a way round the stronger one there.
    "/api/v1/stock-take/transactions/verify": {"POST": "verify"},
    "/api/v1/stock-take/balance":             {"GET": "view"},
    # Everything on one warehouse + floor, for the job card's material tab. A
    # read like latest-stock, so the same gate.
    "/api/v1/stock-take/floor-stock":         {"GET": "view"},
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
    """Posting, exporting and signing off are each reachable on their own."""
    actions = {a for r in ROUTES if (p := _permission_of(r)) for a in (p[1],)}
    assert actions == {"view", "create", "export", "verify"}, actions


def test_posting_and_verifying_are_different_actions():
    """The separation this whole feature exists for.

    If verify collapsed into create, the role that posts an adjustment would also
    approve it, and the sign-off would certify nothing.
    """
    post = _permission_of(_by_path("/api/v1/stock-take/transactions", "POST"))
    ver = _permission_of(_by_path("/api/v1/stock-take/adjustments/verify", "POST"))
    assert post == ("stock_take", "create")
    assert ver == ("stock_take", "verify")
    assert post[1] != ver[1]


def _by_path(path: str, method: str) -> APIRoute:
    for r in ROUTES:
        if r.path == path and method in r.methods:
            return r
    raise AssertionError("no %s %s" % (method, path))


def test_no_route_still_uses_bare_authentication():
    from app.modules.auth.middleware import get_current_user

    for r in ROUTES:
        calls = {d.call for d in r.dependant.dependencies}
        assert get_current_user not in calls, f"{r.path} still uses get_current_user"
