"""Merged process run: Distribute shares ALL the output, so the card can close.

Before: a packaging card already fed (carried_qty_kg > 0) was skipped and no card
got more than its planned qty. Output above the planned total never moved, the
card kept showing DISTRIBUTE TO PACKAGING, pressing it returned "All packaging
cards already fed." with HTTP 200, the screen said "Distributed", and CLOSE JC
never appeared.

Agreed 2026-10-24: share the output across the products in proportion to their
planned qty. Each press tops every open product up to produced x its share minus
what it was already sent; a finished packaging card gets nothing more (its share
goes to the open ones); nothing left is an error, never a silent "done".
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2

PROC = 900


def _consumer(jid, planned, status="unlocked"):
    return {"job_card_id": jid, "plan_line_id": jid * 10, "planned_qty_kg": planned,
            "planned_qty_units": 100, "carried_qty_kg": 0, "status": status,
            "locked_reason": None}


class _Conn:
    def __init__(self, *, produced, consumers, sent=None, dispatched=None):
        self.produced, self.consumers = produced, consumers
        self.sent = dict(sent or {})                       # consumer id -> kg already sent
        self.dispatched = sum(self.sent.values()) if dispatched is None else dispatched
        self.fed: dict[int, float] = {}
        self.units: dict[int, object] = {}

    async def fetchrow(self, sql, *args):
        if "FROM   job_card_v2" in sql and "process_group_id" in sql and "FOR    UPDATE" in sql:
            return {"job_card_id": PROC, "output_kind": "SFG", "output_code": "SFG-ALM",
                    "fg_sku_name": "Almonds", "entity": "cfpl", "status": "completed",
                    "dispatched_to_next_kg": self.dispatched, "process_group_id": 7}
        if "INSERT INTO job_card_partial_dispatch_v2" in sql:
            to, qty, units = args[2], args[3], args[4]
            self.fed[to] = round(self.fed.get(to, 0) + qty, 3)
            self.units[to] = units
            return {"dispatch_id": args[0]}
        return None

    async def fetchval(self, sql, *args):
        if "FROM job_card_output_v2" in sql:
            return self.produced
        return None

    async def fetch(self, sql, *args):
        if "prev_job_card_id=$1" in sql.replace(" ", ""):
            return self.consumers
        if "FROM job_card_partial_dispatch_v2" in sql:
            return [{"to_job_card_id": k, "sent_kg": v} for k, v in self.sent.items()]
        return []

    async def execute(self, sql, *args):
        return "UPDATE 1"

    def is_in_transaction(self):
        return True

    def transaction(self):
        class _Tx:
            async def __aenter__(_s): return None
            async def __aexit__(_s, *exc): return False
        return _Tx()


@pytest.fixture(autouse=True)
def _no_wip(monkeypatch):
    async def _materialise(conn, **kw): return None
    monkeypatch.setattr(jcv2, "materialise_wip_dispatch", _materialise)


async def _go(conn):
    return await jcv2.dispatch_process_group(conn, process_job_card_id=PROC, dispatched_by="Ravi K")


@pytest.mark.asyncio
async def test_output_equal_to_plan_feeds_each_its_plan():
    conn = _Conn(produced=100, consumers=[_consumer(1, 60), _consumer(2, 40)])
    out = await _go(conn)
    assert conn.fed == {1: 60.0, 2: 40.0}
    assert out["remaining_kg"] == 0


@pytest.mark.asyncio
async def test_extra_output_is_shared_in_proportion():
    """The audit's loop: 120 kg produced against 100 planned."""
    conn = _Conn(produced=120, consumers=[_consumer(1, 60), _consumer(2, 40)])
    await _go(conn)
    assert conn.fed == {1: 72.0, 2: 48.0}


@pytest.mark.asyncio
async def test_output_raised_after_a_distribution_tops_up():
    conn = _Conn(produced=120, consumers=[_consumer(1, 60), _consumer(2, 40)],
                 sent={1: 60.0, 2: 40.0})
    await _go(conn)
    assert conn.fed == {1: 12.0, 2: 8.0}
    # A top-up carries no piece count: the units went with the first feed.
    assert conn.units == {1: None, 2: None}


@pytest.mark.asyncio
async def test_less_output_than_planned_is_scaled_down():
    conn = _Conn(produced=80, consumers=[_consumer(1, 60), _consumer(2, 40)])
    await _go(conn)
    assert conn.fed == {1: 48.0, 2: 32.0}


@pytest.mark.asyncio
async def test_a_finished_packaging_card_gets_nothing_more():
    conn = _Conn(produced=120, consumers=[_consumer(1, 60), _consumer(2, 40, status="closed")],
                 sent={1: 60.0, 2: 40.0})
    await _go(conn)
    assert conn.fed == {1: 20.0}


@pytest.mark.asyncio
async def test_rounding_never_leaves_a_remainder():
    conn = _Conn(produced=100, consumers=[_consumer(1, 1), _consumer(2, 1), _consumer(3, 1)])
    await _go(conn)
    assert sum(conn.fed.values()) == pytest.approx(100.0, abs=1e-9)
    assert sorted(conn.fed.values()) == [33.333, 33.333, 33.334]


@pytest.mark.asyncio
async def test_nothing_left_is_said_not_shown_as_done():
    conn = _Conn(produced=100, consumers=[_consumer(1, 60), _consumer(2, 40)],
                 sent={1: 60.0, 2: 40.0})
    out = await _go(conn)
    assert out["error"] == "nothing_to_distribute"
    assert conn.fed == {}


@pytest.mark.asyncio
async def test_every_packaging_card_finished_says_so():
    conn = _Conn(produced=120, consumers=[_consumer(1, 60, "closed"), _consumer(2, 40, "completed")],
                 sent={1: 60.0, 2: 40.0})
    out = await _go(conn)
    assert out["error"] == "no_open_packaging"
    assert "20.000 kg" in out["message"]
    assert conn.fed == {}
