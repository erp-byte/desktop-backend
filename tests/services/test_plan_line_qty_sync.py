"""A plan line's qty change moves its SO reservation; no cards on a cancelled
plan or without a floor (audit 2026-10-23).

Before: PUT /plans-v2/lines/{id} changed planned_qty on the line only, so the
SO's reserved qty stayed at the old figure and SO Creation showed the wrong
pending qty — then cancel gave back the NEW figure. Create Job Card still worked
on a cancelled plan, and a process with a blank floor made a card no floor sees.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services import plan_v2
from app.modules.production.services import so_reservation

LINE = 6101


class _LineConn:
    def __init__(self, *, kg=100.0, units=400.0, status="approved", carded=0.0, fids=(11,)):
        self.before = {"planned_qty_kg": kg, "planned_qty_units": units,
                       "linked_so_fulfillment_ids": list(fids), "plan_status": status,
                       "carded_kg": carded}
        self.updated: tuple | None = None

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if "carded_kg" in s:
            return self.before
        if s.startswith("UPDATE production_plan_line_v2"):
            self.updated = args
            new = dict(self.before)
            cols = [c.split(" = ")[0] for c in s.split(" SET ")[1].split(" WHERE ")[0].split(", ")]
            new.update(dict(zip(cols, args)))
            return new
        raise AssertionError(s[:70])


@pytest.fixture
def moves(monkeypatch):
    calls = []

    async def move(conn, plan_line_id, fids, *, delta_kg, delta_units):
        calls.append((plan_line_id, list(fids), round(delta_kg, 3), round(delta_units, 3)))
        return {}
    monkeypatch.setattr(so_reservation, "move", move)
    return calls


@pytest.mark.asyncio
async def test_a_qty_change_moves_the_so_reservation_by_the_difference(moves):
    out = await plan_v2.update_plan_line(_LineConn(), LINE, {"planned_qty_kg": 80.0,
                                                             "planned_qty_units": 320.0})
    assert out["updated"] is True
    assert moves == [(LINE, [11], -20.0, -80.0)]


@pytest.mark.asyncio
async def test_a_rise_reserves_more(moves):
    await plan_v2.update_plan_line(_LineConn(), LINE, {"planned_qty_kg": 125.0})
    assert moves == [(LINE, [11], 25.0, 0.0)]


@pytest.mark.asyncio
async def test_other_fields_leave_the_reservation_alone(moves):
    conn = _LineConn()
    out = await plan_v2.update_plan_line(conn, LINE, {"area": "Bay 3"})
    assert out["updated"] is True and moves == []


@pytest.mark.asyncio
async def test_not_below_what_the_job_cards_carry(moves):
    conn = _LineConn(carded=60.0)
    out = await plan_v2.update_plan_line(conn, LINE, {"planned_qty_kg": 50.0})
    assert out["error"] == "below_carded"
    assert "60 kg" in out["message"]
    assert conn.updated is None and moves == []


@pytest.mark.parametrize("status", ["cancelled", "executed"])
@pytest.mark.asyncio
async def test_not_on_a_closed_plan(moves, status):
    conn = _LineConn(status=status)
    out = await plan_v2.update_plan_line(conn, LINE, {"planned_qty_kg": 50.0})
    assert out["error"] == "plan_closed"
    assert conn.updated is None and moves == []


# ── Create Job Card / merged run ───────────────────────────────────────────

class _NoDb:
    async def fetchrow(self, *a): raise AssertionError("refused before the database")
    fetch = fetchval = execute = fetchrow


@pytest.mark.asyncio
async def test_a_process_without_a_floor_is_refused():
    out = await jcv2.create_job_cards_for_line(
        _NoDb(), LINE, qty_kg=100,
        wip_steps=[{"process": "Roasting", "floor": "Roasting Area"}, {"process": "Sorting", "floor": " "}],
        pkg_floor="FG store")
    assert out == {"error": "missing_floor", "message": "Pick a floor for Sorting."}


class _CancelledPlanConn(_NoDb):
    async def fetchrow(self, sql, *args):
        return {"plan_line_id": LINE, "plan_id": 1, "bom_id": None, "fg_sku_name": "x",
                "customer_name": None, "planned_qty_kg": 100, "entity": "cfpl",
                "warehouse": "W202", "plan_status": "cancelled"}


@pytest.mark.asyncio
async def test_no_cards_on_a_cancelled_plan():
    out = await jcv2.create_job_cards_for_line(
        _CancelledPlanConn(), LINE, qty_kg=100,
        wip_steps=[{"process": "Roasting", "floor": "Roasting Area"}], pkg_floor="FG store")
    assert out["error"] == "plan_closed"


@pytest.mark.asyncio
async def test_a_merged_run_process_without_a_floor_is_refused():
    out = await jcv2.create_merged_process_run(
        _NoDb(), plan_line_ids=[1, 2], wip_steps=[{"process": "Roasting", "floor": ""}],
        per_member=[{"plan_line_id": 1, "pkg_floor": "FG store"},
                    {"plan_line_id": 2, "pkg_floor": "FG store"}])
    assert out["error"] == "missing_floor"


class _MergeRowsConn(_NoDb):
    async def fetch(self, sql, *args):
        return [{"plan_line_id": 1, "plan_id": 41, "plan_status": "approved"},
                {"plan_line_id": 2, "plan_id": 42, "plan_status": "cancelled"}]


@pytest.mark.asyncio
async def test_a_merged_run_with_a_cancelled_plan_is_refused():
    out = await jcv2.create_merged_process_run(
        _MergeRowsConn(), plan_line_ids=[1, 2],
        wip_steps=[{"process": "Roasting", "floor": "Roasting Area"}],
        per_member=[{"plan_line_id": 1, "pkg_floor": "FG store"},
                    {"plan_line_id": 2, "pkg_floor": "FG store"}])
    assert out["error"] == "plan_closed"
    assert "Plan 42 is cancelled" in out["message"]
