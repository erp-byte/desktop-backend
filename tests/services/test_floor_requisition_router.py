"""/api/v1/floor-requisitions — permission wiring and HTTP shape.

The wiring half walks the mounted routes and reads the permission each one
actually resolved (the test_stock_take_rbac approach): adding an endpoint means
adding a row to EXPECTED, deliberately.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.routing import APIRoute

from app.modules.floor_requisition import router as R
from app.modules.floor_requisition.services import notify_service
from app.modules.floor_requisition.services import requisition_service as svc

EXPECTED = {
    "/api/v1/floor-requisitions": {"GET": "view", "POST": "create"},
    "/api/v1/floor-requisitions/{requisition_id}/issue": {"POST": "issue"},
    "/api/v1/floor-requisitions/{requisition_id}/receive": {"POST": "receive"},
    "/api/v1/floor-requisitions/{requisition_id}/cancel": {"POST": "cancel"},
    "/api/v1/floor-requisitions/{requisition_id}/boxes": {"GET": "view"},
    "/api/v1/floor-requisitions/{requisition_id}/boxes/scan": {"POST": "issue"},
    "/api/v1/floor-requisitions/{requisition_id}/boxes/print": {"POST": "issue"},
    "/api/v1/floor-requisitions/{requisition_id}/boxes/{box_code}": {"DELETE": "issue"},
}
ROUTES = [r for r in R.router.routes if isinstance(r, APIRoute)]


def _permission_of(route: APIRoute):
    for dep in route.dependant.dependencies:
        fn = dep.call
        code, cells = getattr(fn, "__code__", None), getattr(fn, "__closure__", None)
        if not code or not cells:
            continue
        names = dict(zip(code.co_freevars, (c.cell_contents for c in cells)))
        if "module" in names and "action" in names:
            return names["module"], names.get("sub_module"), names["action"]
    return None


def test_every_route_is_accounted_for():
    seen = {(r.path, m) for r in ROUTES for m in r.methods if m != "HEAD"}
    want = {(p, m) for p, ms in EXPECTED.items() for m in ms}
    assert seen == want


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{sorted(r.methods)}{r.path}")
def test_route_requires_its_floor_requisitions_permission(route):
    got = _permission_of(route)
    assert got is not None, f"{route.path} has no require_permission dependency"
    for method in route.methods - {"HEAD"}:
        assert got == ("production", "floor_requisitions", EXPECTED[route.path][method])


def test_the_router_is_mounted_in_the_app():
    main = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app.modules.floor_requisition.router import router as floor_requisition_router" in main
    assert "app.include_router(floor_requisition_router)" in main


def test_the_body_cannot_name_who_did_it():
    for model in (R.RaiseBody, R.IssueBody, R.CancelBody, R.ScanBoxBody, R.PrintBoxesBody, R.PrintBoxLine):
        assert not {"raised_by", "issued_by", "received_by", "cancelled_by", "recorded_by",
                    "created_by"} & set(model.model_fields)


class _Ctx:
    def __init__(self, value, on_exit=None):
        self.value, self.on_exit = value, on_exit

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        if self.on_exit:
            self.on_exit(exc[0] is None)
        return False


class _Conn:
    def __init__(self):
        self.committed = False

    def transaction(self):
        return _Ctx(self, on_exit=lambda ok: setattr(self, "committed", ok))


def _request():
    conn = _Conn()
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn), conn=conn)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))


def test_a_refusal_becomes_error_message_details(monkeypatch):
    async def refuse(conn, user, **kw):
        raise svc.RequisitionError(409, "open_requisition_exists",
                                   "Requisition 87654321 for this article is still waiting for store.",
                                   requisition_id=87654321)

    monkeypatch.setattr(svc, "raise_requisition", refuse)
    body = R.RaiseBody(job_card_id=12345678, material_sku_name="Pista", requested_qty="5")
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.raise_floor_requisition(_request(), body, tasks, user=object()))
    assert exc.value.status_code == 409
    # A refused raise tells store nothing.
    assert tasks.tasks == []
    assert exc.value.detail == {
        "error": "open_requisition_exists",
        "message": "Requisition 87654321 for this article is still waiting for store.",
        "details": {"requisition_id": 87654321},
    }


def test_issue_passes_the_path_id_and_body_through(monkeypatch):
    seen = {}

    async def issue(conn, user, requisition_id, **kw):
        seen.update(requisition_id=requisition_id, user=user, **kw)
        return {"requisition_id": requisition_id, "status": "issued"}

    monkeypatch.setattr(svc, "issue_requisition", issue)
    user = object()
    out = asyncio.run(R.issue_floor_requisition(
        _request(), 87654321, R.IssueBody(issued_qty=500, issue_note="part"), user=user))
    assert out["status"] == "issued"
    assert seen == {"requisition_id": 87654321, "user": user, "issued_qty": 500.0, "issue_note": "part"}


class _WatchedTasks(BackgroundTasks):
    """Records whether the raise transaction had already committed when the
    notification was scheduled."""

    def __init__(self, conn):
        super().__init__()
        self.conn, self.committed_when_added = conn, None

    def add_task(self, func, *args, **kwargs):
        self.committed_when_added = self.conn.committed
        super().add_task(func, *args, **kwargs)


def test_a_committed_raise_schedules_the_store_notice_after_commit(monkeypatch):
    row = {"requisition_id": 87654321, "job_card_id": 12345678, "warehouse": "W202", "floor": "First Floor"}

    async def raise_ok(conn, user, **kw):
        return row

    called = []

    async def notifier(pool, requisition):  # must not run inside the request
        called.append(requisition)

    monkeypatch.setattr(svc, "raise_requisition", raise_ok)
    monkeypatch.setattr(notify_service, "notify_store_of_raise", notifier)
    req = _request()
    tasks = _WatchedTasks(req.app.state.db_pool.conn)
    body = R.RaiseBody(job_card_id=12345678, material_sku_name="Pista", requested_qty="5")
    out = asyncio.run(R.raise_floor_requisition(req, body, tasks, user=object()))

    assert out is row
    assert len(tasks.tasks) == 1 and tasks.committed_when_added is True
    task = tasks.tasks[0]
    assert task.func is notifier and task.args == (req.app.state.db_pool, row)
    assert called == []          # only runs once the response has gone
    asyncio.run(tasks())
    assert called == [row]


# ── boxes (Stores → Production Indents → Scan) ──
from app.modules.floor_requisition.services import box_service as box_svc  # noqa: E402


class _TxConn(_Conn):
    def __init__(self):
        super().__init__()
        self.opened = 0
        self.tx_kwargs: list[dict] = []

    def transaction(self, **kw):
        self.opened += 1
        self.tx_kwargs.append(kw)
        return super().transaction()


def _box_request():
    conn = _TxConn()
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn), conn=conn)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool))), conn


def test_scan_runs_outside_a_transaction(monkeypatch):
    seen = {}

    async def scan(conn, user, requisition_id, *, code):
        seen.update(requisition_id=requisition_id, code=code)
        return {"box_code": "BX-1"}

    monkeypatch.setattr(box_svc, "scan_box", scan)
    req, conn = _box_request()
    out = asyncio.run(R.scan_requisition_box(req, 27385955, R.ScanBoxBody(code='{"tx":"T","bi":"BX-1"}'),
                                             user=object()))
    assert out == {"box_code": "BX-1"} and conn.opened == 0
    assert seen == {"requisition_id": 27385955, "code": '{"tx":"T","bi":"BX-1"}'}


def test_print_runs_in_a_transaction_and_passes_the_boxes(monkeypatch):
    seen = {}

    async def print_boxes(conn, user, requisition_id, **kw):
        seen.update(requisition_id=requisition_id, **kw)
        return {"requisition_id": requisition_id, "boxes": []}

    monkeypatch.setattr(box_svc, "print_boxes", print_boxes)
    req, conn = _box_request()
    body = R.PrintBoxesBody(article="Seeds", stock_type="Off Grade/Rejection",
                            boxes=[R.PrintBoxLine(box_number=2, net_weight=9.5, lot_number="L1")])
    asyncio.run(R.print_requisition_boxes(req, 27385955, body, user=object()))
    assert conn.opened == 1
    assert seen == {"requisition_id": 27385955, "article": "Seeds", "stock_type": "Off Grade/Rejection",
                    "boxes": [{"box_number": 2, "net_weight": 9.5, "gross_weight": None, "count": None,
                               "lot_number": "L1"}]}


def test_remove_runs_in_a_transaction(monkeypatch):
    async def remove(conn, user, requisition_id, box_code):
        return {"requisition_id": requisition_id, "box_code": box_code, "removed": True}

    monkeypatch.setattr(box_svc, "remove_box", remove)
    req, conn = _box_request()
    out = asyncio.run(R.remove_requisition_box(req, 27385955, "BX-1", user=object()))
    assert out["removed"] is True and conn.opened == 1


def test_a_box_refusal_becomes_error_message_details(monkeypatch):
    async def scan(conn, user, requisition_id, *, code):
        raise svc.RequisitionError(409, "duplicate_box", "Box BX-1 is already on request 27385955.",
                                   requisition_id=27385955, box_code="BX-1")

    monkeypatch.setattr(box_svc, "scan_box", scan)
    req, _ = _box_request()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.scan_requisition_box(req, 27385955, R.ScanBoxBody(code="BX-1"), user=object()))
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "duplicate_box"
    assert exc.value.detail["details"] == {"requisition_id": 27385955, "box_code": "BX-1"}


def test_print_body_limits():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        R.PrintBoxesBody(article="S", boxes=[])
    with pytest.raises(pydantic.ValidationError):
        R.PrintBoxesBody(article="S", stock_type="Rotten", boxes=[R.PrintBoxLine(box_number=1, net_weight=1)])
    with pytest.raises(pydantic.ValidationError):
        R.PrintBoxLine(box_number=0, net_weight=1)


def test_the_box_list_passes_page_size_and_find_through(monkeypatch):
    seen = {}

    async def list_boxes(conn, user, requisition_id, **kw):
        seen.update(requisition_id=requisition_id, **kw)
        return {"boxes": []}

    monkeypatch.setattr(box_svc, "list_boxes", list_boxes)
    req, conn = _box_request()
    asyncio.run(R.list_requisition_boxes(req, 27385955, page=3, page_size=25, find="9568129-1", user=object()))
    assert seen == {"requisition_id": 27385955, "page": 3, "page_size": 25, "find": "9568129-1"}
    # One snapshot for the page, the totals and find: a scan landing between the
    # reads must not move the found box off the page that names it.
    assert conn.tx_kwargs == [{"isolation": "repeatable_read", "readonly": True}]


def test_the_box_list_query_limits():
    import inspect
    params = inspect.signature(R.list_requisition_boxes).parameters
    page, size, find = params["page"].default, params["page_size"].default, params["find"].default
    assert page.default == 1 and any(getattr(m, "ge", None) == 1 for m in page.metadata)
    assert size.default == 10
    assert any(getattr(m, "ge", None) == 1 for m in size.metadata)
    assert any(getattr(m, "le", None) == box_svc.MAX_PAGE_SIZE for m in size.metadata)
    assert find.default is None and any(getattr(m, "max_length", None) == 2000 for m in find.metadata)
