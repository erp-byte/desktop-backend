"""Plan cancel / delete / approve / line-edit routes (agreed 2026-10-24).

Before (audit 2026-10-23): cancel and delete were admin-only on top of the
permission check, though the planner role holds plans › cancel and plans ›
delete — and the plan screen told planners to "cancel the plan and create a new
revision". Now admins and planners may, a non-admin only within their own
factory. A refusal (work started, articles incomplete, SO short) is a 4xx raised
inside the transaction, so it rolls back. Services are faked.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.modules.production import router as R
from app.modules.production.services import mail_service, plan_v2
from app.modules.production.services.so_reservation import OverAllocation

PLAN = 9003
PLANNER = SimpleNamespace(is_admin=False, allowed_floors=[], allowed_warehouses=["W202"],
                          full_name="Asha P", email="a@candorfoods.in", phone="9800000000", user_id=21)


class _Conn:
    def __init__(self, warehouse="W-202"):
        self.warehouse = warehouse
        self.tx_exits: list = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self): return None

            async def __aexit__(self, exc_type, *rest):
                conn.tx_exits.append(exc_type)
                return False
        return _Tx()

    async def fetchval(self, sql, *args):
        assert "SELECT warehouse FROM production_plan_v2" in sql
        return self.warehouse


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn))))


def _cancel(conn, reason="Customer cancelled"):
    return asyncio.run(R.cancel_plan_v2(_req(conn), PLAN, R.PlanV2Cancel(reason=reason), user=PLANNER))


def test_a_planner_can_cancel_a_plan_in_their_factory(monkeypatch):
    seen = {}

    async def cancel(conn, plan_id, reason, *, cancelled_by=None):
        seen.update(plan_id=plan_id, reason=reason, by=cancelled_by)
        return {"cancelled": True, "job_cards_cancelled": 3, "released": {"kg": 100.0, "units": 0.0}}
    monkeypatch.setattr(plan_v2, "cancel_plan", cancel)

    conn = _Conn()
    out = _cancel(conn)
    assert out["job_cards_cancelled"] == 3
    # The canceller is the logged-in user, not anything the browser sent.
    assert seen == {"plan_id": PLAN, "reason": "Customer cancelled", "by": "Asha P"}
    assert conn.tx_exits == [None]


def test_not_a_plan_outside_their_factory(monkeypatch):
    called = []

    async def cancel(*a, **kw): called.append(1); return {"cancelled": True}
    monkeypatch.setattr(plan_v2, "cancel_plan", cancel)

    with pytest.raises(HTTPException) as exc:
        _cancel(_Conn(warehouse="A185"))
    assert exc.value.status_code == 403
    assert called == []


def test_started_work_is_a_409_and_rolls_back(monkeypatch):
    async def cancel(*a, **kw):
        return {"error": "plan_has_started_cards",
                "message": "Work has started on this plan, so it can't be cancelled: PLAN-1-L1-S1 (in progress)."}
    monkeypatch.setattr(plan_v2, "cancel_plan", cancel)

    conn = _Conn()
    with pytest.raises(HTTPException) as exc:
        _cancel(conn)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "plan_has_started_cards"
    assert "PLAN-1-L1-S1" in exc.value.detail["message"]
    assert conn.tx_exits[0] is not None


def test_already_cancelled_is_still_a_404(monkeypatch):
    async def cancel(*a, **kw): return {"error": "not_found_or_already_cancelled"}
    monkeypatch.setattr(plan_v2, "cancel_plan", cancel)
    with pytest.raises(HTTPException) as exc:
        _cancel(_Conn())
    assert exc.value.status_code == 404
    assert exc.value.detail["message"] == "Plan not found or already cancelled"


def test_a_planner_can_delete_and_the_name_comes_from_the_login(monkeypatch):
    seen = {}

    async def delete(conn, plan_id, *, reason, deleted_by):
        seen["by"] = deleted_by
        return {"deleted": True, "plan": {"status": "cancelled", "warehouse": "W-202"},
                "job_cards_cancelled": 2, "released": {"kg": 50.0, "units": 0.0}}

    async def email(conn, **kw):
        seen["email_by"] = kw["deleted_by"]
        return 4
    monkeypatch.setattr(plan_v2, "delete_plan", delete)
    monkeypatch.setattr(mail_service, "send_plan_deletion_email", email)

    body = R.PlanV2Delete(reason="Entered twice", deleted_by="Someone Else")
    out = asyncio.run(R.delete_plan_v2(_req(_Conn()), PLAN, body, user=PLANNER))
    assert out["deleted"] is True and out["admin_email_count"] == 4
    assert out["job_cards_cancelled"] == 2
    assert seen == {"by": "Asha P", "email_by": "Asha P"}


def test_a_refused_delete_sends_no_email(monkeypatch):
    sent = []

    async def delete(*a, **kw): return {"error": "plan_has_started_cards", "message": "Work has started"}
    async def email(conn, **kw): sent.append(1); return 1
    monkeypatch.setattr(plan_v2, "delete_plan", delete)
    monkeypatch.setattr(mail_service, "send_plan_deletion_email", email)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.delete_plan_v2(_req(_Conn()), PLAN, R.PlanV2Delete(reason="x"), user=PLANNER))
    assert exc.value.status_code == 409
    assert sent == []


def test_an_incomplete_plan_is_a_409_naming_the_articles(monkeypatch):
    async def approve(conn, plan_id, approved_by):
        return {"error": "plan_incomplete", "message": "Not approved. Fix these articles first: X",
                "problems": [{"plan_line_id": 1, "fg_sku_name": "X", "problem": "no_steps"}]}
    monkeypatch.setattr(plan_v2, "approve_plan", approve)

    conn = _Conn()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.approve_plan_v2(_req(conn), PLAN, R.PlanV2Approve(approved_by="Asha P"),
                                      BackgroundTasks(), user=PLANNER))
    assert exc.value.status_code == 409
    assert exc.value.detail["problems"][0]["fg_sku_name"] == "X"
    assert conn.tx_exits[0] is not None


def _edit_line(conn, **fields):
    return asyncio.run(R.update_plan_line_v2(_req(conn), 51, R.PlanLineV2Patch(**fields), user=PLANNER))


def test_a_line_rise_the_so_cannot_cover_is_a_400_and_rolls_back(monkeypatch):
    async def update(conn, plan_line_id, fields):
        raise OverAllocation("Would over-allocate fulfillment [11]: requested 25.0 kg")
    monkeypatch.setattr(plan_v2, "update_plan_line", update)

    conn = _Conn()
    with pytest.raises(HTTPException) as exc:
        _edit_line(conn, planned_qty_kg=125)
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "over_allocation"
    assert conn.tx_exits[0] is OverAllocation


def test_a_line_below_its_cards_is_a_409(monkeypatch):
    async def update(conn, plan_line_id, fields):
        return {"error": "below_carded", "message": "60 kg of this article already has job cards"}
    monkeypatch.setattr(plan_v2, "update_plan_line", update)
    with pytest.raises(HTTPException) as exc:
        _edit_line(_Conn(), planned_qty_kg=50)
    assert exc.value.status_code == 409


def test_a_missing_line_is_still_a_404(monkeypatch):
    async def update(conn, plan_line_id, fields): return {"error": "not_found"}
    monkeypatch.setattr(plan_v2, "update_plan_line", update)
    with pytest.raises(HTTPException) as exc:
        _edit_line(_Conn(), area="Bay 3")
    assert exc.value.status_code == 404
    assert exc.value.detail["message"] == "Plan line not found"
