"""An article can be given job cards again after its cards were cancelled.

Before (audit 2026-10-23): re-creating after a cancel failed with a server error:
  * the create deleted ALL the line's plan steps, but the cancelled cards still
    reference theirs (plan_step_id ... ON DELETE RESTRICT) — foreign-key error;
  * the chain number came from the LIVE heads, so the new chain reused a
    cancelled chain's PLAN-…-S1 / -B{n} number — job_card_number is UNIQUE
    across every row, cancelled included.
Now only unreferenced plan steps are deleted (new steps follow the ones kept),
and the chain number is taken past every number ever used on the line.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2


# ── the chain number ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("numbers,expected", [
    ([], 1),                                                         # never carded
    (["PLAN-7-L5-S1", "PLAN-7-L5-S2"], 2),                           # chain 1 exists (even cancelled)
    (["PLAN-7-L5-S1", "PLAN-7-L5-S2", "PLAN-7-L5-S1-B2"], 3),
    (["PLAN-7-L5-S1-B3"], 4),                                        # the highest wins, gaps or not
    (["PLAN-7-L5-ADD2", "PLAN-7-L5-S1"], 2),                         # a live-edit addition is chain 1
])
def test_the_next_chain_number_is_past_every_number_used(numbers, expected):
    assert jcv2.next_chain_no(numbers) == expected


# ── the plan steps ───────────────────────────────────────────────────────────
class _Conn:
    def __init__(self, kept_max):
        self.kept_max, self.sql = kept_max, []

    async def execute(self, sql, *args):
        self.sql.append(" ".join(sql.split()))
        return "DELETE 2"

    async def fetchval(self, sql, *args):
        self.sql.append(" ".join(sql.split()))
        return self.kept_max


@pytest.mark.asyncio
async def test_first_chain_keeps_steps_cancelled_cards_still_use():
    conn = _Conn(kept_max=3)
    base = await jcv2.reset_line_steps(conn, 5, first_chain=True)
    delete = conn.sql[0]
    assert delete.startswith("DELETE FROM production_plan_step_v2")
    assert "NOT EXISTS (SELECT 1 FROM job_card_v2 j WHERE j.plan_step_id = s.step_id)" in delete
    assert base == 3                    # the new steps follow the ones kept


@pytest.mark.asyncio
async def test_first_chain_on_a_clean_line_starts_at_one():
    assert await jcv2.reset_line_steps(_Conn(kept_max=0), 5, first_chain=True) == 0


@pytest.mark.asyncio
async def test_a_later_chain_deletes_nothing():
    conn = _Conn(kept_max=6)
    assert await jcv2.reset_line_steps(conn, 5, first_chain=False) == 6
    assert not any(s.startswith("DELETE") for s in conn.sql)
