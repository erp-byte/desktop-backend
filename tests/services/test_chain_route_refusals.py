"""Job-card chain routes: a refusal is an error AND rolls the transaction back.

Before: create / Edit (replace) / live edit / dispatch checked the service result
only after the transaction had committed, and mapped only some codes. So
  * create's line_fully_carded / exceeds_balance came back 200 → the screen said
    "Created undefined job cards";
  * Edit with a quantity above the plan deleted every card, the recreate refused
    (exceeds_balance), the delete COMMITTED and the screen said "Updated".
Now the refusal is raised inside the transaction (it exits with the exception →
asyncpg rolls back) and becomes a 4xx. Services are faked.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.modules.production import router as R
from app.modules.production.services import job_card_v2 as jcv2

USER = SimpleNamespace(is_admin=True, allowed_floors=[], allowed_warehouses=[], full_name="Ravi K",
                       email="r@candorfoods.in", phone="9876543210", user_id=8)
LINE = 5101


class _Conn:
    """Records how the transaction ended: committed, or rolled back by an exception."""

    def __init__(self):
        self.tx_exits: list[type | None] = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self): return None

            async def __aexit__(self, exc_type, *rest):
                conn.tx_exits.append(exc_type)
                return False
        return _Tx()

    async def fetch(self, sql, *args):
        return []


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


@pytest.fixture
def conn():
    return _Conn()


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn))))


CREATE_BODY = R.JobCardLineCreate(qty_kg=100, wip_steps=[R.JobCardLineCreateStep(process="Roasting", floor="Roasting Area")],
                                  pkg_floor="FG store")


def _create(conn):
    return asyncio.run(R.create_line_job_cards_v2(_request(conn), LINE, CREATE_BODY, BackgroundTasks(), user=USER))


@pytest.mark.parametrize("code", ["line_fully_carded", "exceeds_balance"])
def test_create_refusals_the_route_used_to_pass_as_200(conn, monkeypatch, code):
    async def no_merge(conn, line, ids): return None
    async def refuse(conn, line, **kw): return {"error": code, "message": "Only 40 kg left on this line"}
    monkeypatch.setattr(jcv2, "consolidate_plan_lines_for_merge", no_merge)
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", refuse)

    with pytest.raises(HTTPException) as exc:
        _create(conn)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == code
    assert exc.value.detail["message"] == "Only 40 kg left on this line"
    assert conn.tx_exits and conn.tx_exits[0] is not None, "the transaction must roll back"


def test_a_refused_merge_rolls_back_before_creating(conn, monkeypatch):
    created = []
    async def merge_conflict(conn, line, ids): return {"error": "merge_conflict", "message": "Different BOMs"}
    async def create(conn, line, **kw): created.append(line); return {"count": 2}
    monkeypatch.setattr(jcv2, "consolidate_plan_lines_for_merge", merge_conflict)
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)

    with pytest.raises(HTTPException) as exc:
        _create(conn)
    assert exc.value.status_code == 409
    assert created == []
    assert conn.tx_exits[0] is not None


def test_create_success_commits_and_returns_the_cards(conn, monkeypatch):
    released = []
    async def no_merge(conn, line, ids): return None
    async def create(conn, line, **kw): return {"count": 2, "job_card_ids": [], "plan_id": 77}
    async def release(conn, plan_id, **kw): released.append(plan_id)
    monkeypatch.setattr(jcv2, "consolidate_plan_lines_for_merge", no_merge)
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)
    monkeypatch.setattr(jcv2, "maybe_release_plan_from_jcs", release)

    out = _create(conn)
    assert out["count"] == 2
    assert released == [77]
    assert conn.tx_exits == [None]          # committed


def test_edit_above_the_plan_rolls_the_delete_back(conn, monkeypatch):
    """The exact audit case: replace deletes, the recreate refuses."""
    async def replace(conn, line, **kw):
        return {"error": "exceeds_balance", "message": "Only 40 kg left on this line"}
    monkeypatch.setattr(jcv2, "replace_job_cards_for_line", replace)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.replace_line_job_cards_v2(_request(conn), LINE, CREATE_BODY, BackgroundTasks(), user=USER))
    assert exc.value.status_code == 409
    assert conn.tx_exits[0] is not None, "the delete must not commit"


def test_live_edit_refusal_rolls_back(conn, monkeypatch):
    async def live(conn, line, **kw): return {"error": "no_wip_steps", "message": "At least one process"}
    monkeypatch.setattr(jcv2, "apply_live_job_card_edits", live)
    body = R.JobCardLineApplyEdits(qty_kg=100, steps=[], pkg_floor="FG store")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.apply_line_job_card_edits_v2(_request(conn), LINE, body, BackgroundTasks(), user=USER))
    assert exc.value.status_code == 400
    assert conn.tx_exits[0] is not None
