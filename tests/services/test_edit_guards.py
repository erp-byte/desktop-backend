"""Edit / live edit refuse articles they would damage — before touching anything.

  * Several partial chains (audit): Edit read all of the line's cards as ONE
    chain, so it mixed them; a plain Save of live edit physically merged them
    into one, and replace deleted every chain and rebuilt one.
  * A merged process run (audit): replace on the primary line failed on the
    packaging cards of the other products; on a member line it silently pulled
    the member out of the run; live edit re-pointed the shared process card at
    one product's packaging so the others were never fed.
Agreed 2026-10-24: block Edit for these, with a clear message.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2

LINE = 5101


class _Conn:
    def __init__(self, *, heads=1, group_id=None):
        self.heads, self.group_id = heads, group_id
        self.executed: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(" ".join(sql.split()))
        return "OK"

    async def fetchrow(self, sql, *args):
        if "process_group_id" in sql and "prev_job_card_id IS NULL" in sql:
            return {"heads": self.heads, "group_id": self.group_id}
        return None

    async def fetch(self, sql, *args):
        # Reaching here means the guard let it through (the real cards query).
        return [{"job_card_id": 1, "status": "unlocked"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["replace", "live"])
async def test_several_chains_are_refused_before_anything_changes(call):
    conn = _Conn(heads=2)
    out = await _call(call, conn)
    assert out["error"] == "multiple_chains"
    assert out["chains"] == 2
    assert "2 separate chains" in out["message"]
    assert not any(s.startswith("DELETE") or s.startswith("UPDATE") for s in conn.executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["replace", "live"])
async def test_a_merged_run_is_refused_before_anything_changes(call):
    conn = _Conn(heads=1, group_id=88)
    out = await _call(call, conn)
    assert out["error"] == "merged_run"
    assert out["process_group_id"] == 88
    assert "merged process run" in out["message"]
    assert not any(s.startswith("DELETE") or s.startswith("UPDATE") for s in conn.executed)


@pytest.mark.asyncio
async def test_one_chain_is_not_blocked():
    assert await jcv2.line_edit_refusal(_Conn(heads=1), LINE) is None
    assert await jcv2.line_edit_refusal(_Conn(heads=0), LINE) is None   # nothing carded yet


async def _call(call, conn):
    if call == "replace":
        return await jcv2.replace_job_cards_for_line(
            conn, LINE, qty_kg=100, wip_steps=[{"process": "Roasting", "floor": "Roasting Area"}],
            pkg_floor="FG store")
    return await jcv2.apply_live_job_card_edits(
        conn, LINE, qty_kg=100, steps=[], pkg_floor="FG store")
