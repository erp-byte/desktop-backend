"""Approve says what it did, and refuses what it can't card (agreed 2026-10-24).

Before (audit 2026-10-23), "Plan approved" showed even when:
  * an article had no process steps — skipped quietly (3 lines on RDS);
  * a step had no floor — copied onto the cards, which no floor then sees
    (8 cards on RDS);
  * any article was already carded — approve then made NOTHING for the others.
Now approve refuses (plan_incomplete, naming each article and what's missing)
before anything is approved, and the fan-out cards each uncarded article,
skipping the carded ones.
"""
from __future__ import annotations

import pytest

from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services import plan_v2

PLAN = 9002


class _ApproveConn:
    def __init__(self, *, status="draft", problem_rows=()):
        self.status = status
        self.problem_rows = list(problem_rows)
        self.approved = False

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT status FROM production_plan_v2"):
            return None if self.status is None else {"status": self.status}
        if s.startswith("UPDATE production_plan_v2"):
            self.approved = True
            return {"plan_id": PLAN, "status": "approved", "approved_by": args[1]}
        raise AssertionError(s[:70])

    async def fetch(self, sql, *args):
        assert "step_count" in sql
        return self.problem_rows


def _problem(lid, name, steps, no_floor=None, pack_steps=1):
    return {"plan_line_id": lid, "fg_sku_name": name, "step_count": steps,
            "pack_steps": pack_steps, "no_floor": no_floor}


@pytest.mark.asyncio
async def test_missing_steps_and_floors_refuse_the_approval(monkeypatch):
    fanned = []
    async def fan_out(conn, plan_id): fanned.append(plan_id); return {"lines": []}
    monkeypatch.setattr(jcv2, "create_job_cards_from_plan", fan_out)

    conn = _ApproveConn(problem_rows=[
        _problem(1, "Almond 500g", 0),
        _problem(2, "Cashew 1kg", 3, ["Sorting", "Packaging"]),
        _problem(3, "Raisin 250g", 2, None),          # complete — not a problem
    ])
    out = await plan_v2.approve_plan(conn, PLAN, "Asha P")
    assert out["error"] == "plan_incomplete"
    assert out["problems"] == [
        {"plan_line_id": 1, "fg_sku_name": "Almond 500g", "problem": "no_steps"},
        {"plan_line_id": 2, "fg_sku_name": "Cashew 1kg", "problem": "no_floor",
         "steps": ["Sorting", "Packaging"]},
    ]
    assert out["message"] == (
        "These articles can't be carded yet: Almond 500g has no process steps; "
        "Cashew 1kg: no floor for Sorting, Packaging. Set the floors on the plan's lines, "
        "or card the article with Create Job Card.")
    assert conn.approved is False and fanned == []


@pytest.mark.asyncio
async def test_steps_left_by_several_cancelled_chains_are_refused():
    """Carded in two parts, both cancelled: the line keeps both chains' steps
    (two packaging steps). One approve chain would run them back to back."""
    conn = _ApproveConn(problem_rows=[_problem(4, "Pista 200g", 4, pack_steps=2)])
    out = await plan_v2.approve_plan(conn, PLAN, "Asha P")
    assert out["problems"] == [{"plan_line_id": 4, "fg_sku_name": "Pista 200g",
                                "problem": "several_chains"}]
    assert "Pista 200g has steps from more than one earlier job card chain" in out["message"]
    assert conn.approved is False


def test_a_long_list_is_cut_short():
    problems = [{"plan_line_id": i, "fg_sku_name": f"SKU {i}", "problem": "no_steps"}
                for i in range(8)]
    msg = plan_v2.incomplete_message(problems)
    assert "SKU 4 has no process steps; and 3 more." in msg
    assert "SKU 5" not in msg


@pytest.mark.asyncio
async def test_a_complete_plan_approves_and_reports_what_was_made(monkeypatch):
    async def fan_out(conn, plan_id):
        return {"plan_id": plan_id, "lines": [
            {"plan_line_id": 1, "job_card_ids": [11, 12, 13]},
            {"plan_line_id": 2, "job_card_ids": [], "skipped": "already_carded"},
            {"plan_line_id": 3, "job_card_ids": [31]},
        ]}
    monkeypatch.setattr(jcv2, "create_job_cards_from_plan", fan_out)

    conn = _ApproveConn(problem_rows=[_problem(1, "Almond 500g", 2)])
    out = await plan_v2.approve_plan(conn, PLAN, "Asha P")
    assert conn.approved is True
    assert out["approved"] is True and out["job_cards_created"] is True
    assert out["job_card_count"] == 4
    assert out["articles_carded"] == 2
    assert out["articles_already_carded"] == 1


@pytest.mark.parametrize("status", [None, "cancelled", "executed"])
@pytest.mark.asyncio
async def test_only_draft_or_approved_plans(status):
    out = await plan_v2.approve_plan(_ApproveConn(status=status), PLAN, "Asha P")
    assert out["error"] == "not_found_or_invalid_status"


# ── the fan-out: per article, not per plan ────────────────────────────────

class _FanOutConn:
    def __init__(self, lines, steps):
        self.lines, self.steps = lines, steps
        self.inserted: list[tuple] = []

    def is_in_transaction(self): return True

    def transaction(self):
        class _Tx:
            async def __aenter__(self): return None
            async def __aexit__(self, *exc): return False
        return _Tx()

    async def fetchrow(self, sql, *args):
        return {"plan_id": PLAN, "entity": "cfpl", "warehouse": "W202"}

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM production_plan_line_v2" in s:
            return self.lines
        if "FROM production_plan_step_v2" in s:
            return self.steps.get(args[0], [])
        raise AssertionError(s[:70])

    async def fetchval(self, sql, *args):
        assert "INSERT INTO job_card_v2" in sql
        self.inserted.append((args[3], args[18]))        # ($4 plan_line_id, $19 floor)
        return 1000 + len(self.inserted)

    async def execute(self, sql, *args):
        return "UPDATE 1"


def _ln(lid, carded=False):
    return {"plan_line_id": lid, "fg_sku_name": f"SKU {lid}", "customer_name": None,
            "bom_id": None, "planned_qty_kg": 100, "planned_qty_units": 400, "carded": carded}


def _st(order, floor):
    return {"step_id": order * 10, "step_order": order, "process_name": f"P{order}",
            "stage": "sorting", "floor": floor}


@pytest.fixture
def no_seam(monkeypatch):
    async def none(conn, bom_id): return None
    monkeypatch.setattr(jcv2, "_resolve_sfg_seam_code", none)


@pytest.mark.asyncio
async def test_a_carded_article_no_longer_stops_the_others(no_seam):
    conn = _FanOutConn([_ln(1, carded=True), _ln(2)],
                       {1: [_st(1, "Roasting Area")], 2: [_st(1, "Roasting Area"), _st(2, "FG store")]})
    out = await jcv2.create_job_cards_from_plan(conn, PLAN)
    assert "error" not in out
    assert out["lines"][0] == {"plan_line_id": 1, "job_card_ids": [], "skipped": "already_carded"}
    assert len(out["lines"][1]["job_card_ids"]) == 2
    assert conn.inserted == [(2, "Roasting Area"), (2, "FG store")]


@pytest.mark.asyncio
async def test_a_step_without_a_floor_makes_no_cards(no_seam):
    conn = _FanOutConn([_ln(1), _ln(2)],
                       {1: [_st(1, "Roasting Area"), _st(2, "  ")], 2: []})
    out = await jcv2.create_job_cards_from_plan(conn, PLAN)
    assert [ln.get("skipped") for ln in out["lines"]] == ["no_floor", "no_steps"]
    assert conn.inserted == []
