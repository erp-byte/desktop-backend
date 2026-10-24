"""What a plan line holds on its SO rows moves with every qty change.

Before (audit 2026-10-23): a line qty edit changed the line only, cancel then
gave back the edited qty, and a line tied to several SO rows (a same-article
merge) took its WHOLE qty off EVERY row on cancel / edit. so_reservation splits a
multi-row line's change across its rows instead; one-row lines (806 of 845 live
lines on RDS) behave exactly as before.
"""
from __future__ import annotations

import asyncpg
import pytest

from app.modules.production.services import so_reservation as R

LINE = 7001


class _Conn:
    """so_fulfillment_v2 rows by id: planned / pending / what other one-row lines hold."""

    def __init__(self, rows, *, check_fails_on=None):
        self.rows = rows
        self.updates: list[tuple] = []
        self.check_fails_on = check_fails_on
        self.fetched_with: list[tuple] = []

    async def fetch(self, sql, *args):
        assert "FROM so_fulfillment_v2 sf" in sql
        fids, line = args
        self.fetched_with.append((list(fids), line))
        # The database hands rows back in its own order, not the line's.
        return [{"fid": f, **self.rows[f]} for f in reversed(fids) if f in self.rows]

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        assert s.startswith("UPDATE so_fulfillment_v2")
        kg, units, fid = args
        if fid == self.check_fails_on:
            raise asyncpg.CheckViolationError('violates check constraint "chk_pending_kg_nonneg"')
        self.updates.append(("take" if "+ $1" in s else "give", fid, kg, units))
        return "UPDATE 1"


def _row(planned_kg=0.0, pending_kg=0.0, others_kg=0.0, planned_units=0.0,
         pending_units=0.0, others_units=0.0):
    return {"planned_qty_kg": planned_kg, "planned_qty_units": planned_units,
            "pending_qty_kg": pending_kg, "pending_qty_units": pending_units,
            "others_kg": others_kg, "others_units": others_units}


# ── the splits ────────────────────────────────────────────────────────────

def test_release_splits_by_what_each_row_holds():
    assert R.split_release(150, {1: 100, 2: 50}) == {1: 100, 2: 50}
    assert R.split_release(30, {1: 100, 2: 50}) == {1: 20, 2: 10}


def test_release_never_takes_more_than_the_rows_hold():
    assert R.split_release(500, {1: 100, 2: 50}) == {1: 100, 2: 50}
    assert R.split_release(10, {1: 0, 2: -5}) == {}


def test_release_rounding_lands_on_the_last_row():
    out = R.split_release(1, {1: 1, 2: 1, 3: 1})
    assert out == {1: 0.333, 2: 0.333, 3: 0.334}
    assert round(sum(out.values()), 3) == 1


def test_reserve_fills_each_row_in_turn():
    assert R.split_reserve(120, [(1, 100), (2, 50)]) == {1: 100, 2: 20}
    assert R.split_reserve(40, [(1, 100), (2, 50)]) == {1: 40}


def test_reserve_is_none_when_the_rows_havent_enough():
    assert R.split_reserve(151, [(1, 100), (2, 50)]) is None


def test_no_ordered_units_means_no_unit_limit():
    assert R.split_reserve(500, [(1, None)]) == {1: 500}


# ── one SO row: exactly as before ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_row_release_gives_back_the_whole_change_without_a_lookup():
    conn = _Conn({})
    out = await R.release(conn, LINE, [11], kg=40, units=160)
    assert conn.fetched_with == []
    assert conn.updates == [("give", 11, 40.0, 160.0)]
    assert R.total(out) == (40.0, 160.0)


@pytest.mark.asyncio
async def test_one_row_over_allocation_is_refused():
    conn = _Conn({}, check_fails_on=11)
    with pytest.raises(R.OverAllocation, match="over-allocate"):
        await R.reserve(conn, [11], kg=40, units=0)


# ── several SO rows: each gives back its own share ────────────────────────

@pytest.mark.asyncio
async def test_a_merged_line_gives_each_row_back_its_own_share():
    """Line = 100 kg from SO row 11 + 50 kg from row 12. Row 12 also holds 30 kg
    for another plan's line. Cancelling used to take 150 off BOTH rows."""
    conn = _Conn({11: _row(planned_kg=100), 12: _row(planned_kg=80, others_kg=30)})
    out = await R.release(conn, LINE, [11, 12], kg=150, units=0)
    assert conn.fetched_with == [([11, 12], LINE)]       # the line itself isn't an "other"
    assert sorted(conn.updates) == [("give", 11, 100.0, 0.0), ("give", 12, 50.0, 0.0)]
    assert R.total(out) == (150.0, 0.0)


@pytest.mark.asyncio
async def test_a_merged_line_qty_cut_comes_off_its_rows_proportionally():
    conn = _Conn({11: _row(planned_kg=100), 12: _row(planned_kg=80, others_kg=30)})
    await R.move(conn, LINE, [11, 12], delta_kg=-30, delta_units=0)
    assert sorted(conn.updates) == [("give", 11, 20.0, 0.0), ("give", 12, 10.0, 0.0)]


@pytest.mark.asyncio
async def test_a_merged_line_qty_rise_fills_the_first_row_with_room():
    conn = _Conn({11: _row(planned_kg=100, pending_kg=5), 12: _row(planned_kg=50, pending_kg=40)})
    await R.move(conn, LINE, [11, 12], delta_kg=25, delta_units=0)
    assert sorted(conn.updates) == [("take", 11, 5.0, 0.0), ("take", 12, 20.0, 0.0)]


@pytest.mark.asyncio
async def test_a_merged_line_rise_beyond_the_rows_is_refused_before_writing():
    conn = _Conn({11: _row(pending_kg=5), 12: _row(pending_kg=10)})
    with pytest.raises(R.OverAllocation):
        await R.reserve(conn, [11, 12], kg=20, units=0)
    assert conn.updates == []


@pytest.mark.asyncio
async def test_kg_and_units_move_apart():
    conn = _Conn({})
    await R.move(conn, LINE, [11], delta_kg=10, delta_units=-4)
    assert conn.updates == [("take", 11, 10.0, 0.0), ("give", 11, 0.0, 4.0)]


@pytest.mark.asyncio
async def test_no_rows_or_no_change_writes_nothing():
    conn = _Conn({})
    assert await R.release(conn, LINE, [], kg=10, units=1) == {}
    assert await R.reserve(conn, [11], kg=0, units=0) == {}
    assert conn.updates == []
