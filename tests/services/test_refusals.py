"""A service refusal ({"error": code, ...}) must never reach the screen as a 200,
and must roll back whatever the service wrote before refusing.

Audit 2026-10-23: the job-card routes checked the result only AFTER their
transaction had committed, and mapped only some codes — so an unmapped refusal
returned HTTP 200 (the screen said "Created undefined job cards"), and Edit with a
quantity above the plan deleted every card, failed to recreate, committed the
delete and still said "Updated".
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.production.services import refusals as rf


def test_success_passes_through():
    ok = {"count": 3, "job_card_ids": [1, 2, 3]}
    assert rf.raise_if_refused(ok) is ok


def test_a_refusal_raises():
    with pytest.raises(rf.ServiceRefusal) as exc:
        rf.raise_if_refused({"error": "exceeds_balance", "message": "Only 40 kg left"})
    assert exc.value.result["error"] == "exceeds_balance"


@pytest.mark.parametrize("code,status", [
    ("line_not_found", 404), ("job_card_not_found", 404), ("no_job_cards", 404),
    ("line_fully_carded", 409), ("exceeds_balance", 409), ("not_editable", 409),
    ("jc_terminal", 409), ("over_dispatch", 409), ("chain_broken", 409),
    ("invalid_qty", 400), ("no_wip_steps", 400), ("missing_pkg_floor", 400),
    ("something_new", 400),     # anything unknown is still an error, never a 200
])
def test_every_code_gets_an_error_status(code, status):
    http = rf.to_http(rf.ServiceRefusal({"error": code, "message": "m"}))
    assert isinstance(http, HTTPException)
    assert http.status_code == status


def test_detail_keeps_the_code_message_and_extras():
    http = rf.to_http(rf.ServiceRefusal(
        {"error": "exceeds_balance", "message": "Only 40 kg left", "remaining_kg": 40.0}))
    assert http.detail == {"error": "exceeds_balance", "message": "Only 40 kg left", "remaining_kg": 40.0}


def test_a_route_can_word_a_code_its_own_way():
    http = rf.to_http(rf.ServiceRefusal({"error": "line_not_found"}),
                      messages={"line_not_found": "Plan line not found"})
    assert http.detail["message"] == "Plan line not found"


def test_a_refusal_without_a_message_still_reads_as_words():
    http = rf.to_http(rf.ServiceRefusal({"error": "line_fully_carded"}))
    assert http.detail["message"] == "Line fully carded"


@pytest.mark.asyncio
async def test_raising_inside_the_transaction_rolls_it_back():
    """The whole point: raised INSIDE `async with conn.transaction()`, the
    transaction exits with the exception (asyncpg rolls back)."""
    exits = []

    class _Tx:
        async def __aenter__(self): return None
        async def __aexit__(self, exc_type, *rest):
            exits.append(exc_type)
            return False

    with pytest.raises(rf.ServiceRefusal):
        async with _Tx():
            rf.raise_if_refused({"error": "exceeds_balance"})
    assert exits == [rf.ServiceRefusal]
