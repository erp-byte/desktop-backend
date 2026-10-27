"""Job card refusals that operators read: a box scanned twice says which card
holds it (and only that refusal is a "duplicate"), a close refused for a missing
sign-off says who must sign. (A cancelled box left out of a batch's box total:
test_fg_from_boxes.)
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import box_scan_service as bss
from app.modules.production.services import job_card_v2 as jcv2

JC = 75009889


class _Ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _request(conn):
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))


# ── a box recorded twice ──────────────────────────────────────────────────────

class DupConn:
    """Just enough of a connection for scan_box to reach its duplicate guard."""

    def __init__(self, holder):
        self.holder = holder
        self.sql: list[str] = []

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.sql.append(s)
        if s.startswith("SELECT 1 FROM job_card_v2"):
            return 1
        if s.startswith("SELECT to_regclass('floor_requisition_box')"):
            return False                                    # no Stores record table
        if s.startswith("SELECT 1 FROM jc_box_scan"):
            return 1
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        self.sql.append(s)
        if s.startswith("SELECT s.job_card_id, j.job_card_number FROM jc_box_scan"):
            assert args == ("9568129-1",)
            return self.holder
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")


def _scan(conn):
    return asyncio.run(bss.scan_box(conn, job_card_id=JC, code="9568129-1", scanned_by="Floor Fay"))


def test_a_box_on_another_card_names_that_card():
    out = _scan(DupConn({"job_card_id": 11111111, "job_card_number": "PLAN-9-L1-S1"}))
    assert out == {"error": "duplicate_box", "box_id": "9568129-1", "job_card_id": 11111111,
                   "job_card_number": "PLAN-9-L1-S1", "same_card": False}
    assert bss.duplicate_box_message(out) == (
        "Box 9568129-1 is already recorded on job card PLAN-9-L1-S1. "
        "Remove it there (✕ in its scanned boxes) to record it here.")


def test_a_box_already_on_this_card_says_so():
    out = _scan(DupConn({"job_card_id": JC, "job_card_number": "PLAN-7-L1-S2"}))
    assert out["same_card"] is True
    assert bss.duplicate_box_message(out) == "Box 9568129-1 is already recorded on this job card."


def test_a_holder_with_no_number_falls_back_to_its_id_or_to_another_card():
    assert "job card 11111111." in bss.duplicate_box_message(
        {"box_id": "B1", "job_card_id": 11111111, "job_card_number": None, "same_card": False})
    assert bss.duplicate_box_message({"box_id": "B1", "job_card_id": None, "same_card": False}) == (
        "Box B1 is already recorded on another job card.")


def _route(monkeypatch, result):
    async def fake(conn, **kw):
        return result
    monkeypatch.setattr(bss, "scan_box", fake)
    user = SimpleNamespace(full_name="Floor Fay", phone=None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(PR.create_box_scan(_request(object()), JC, PR.BoxScanRequest(code="9568129-1"), user=user))
    return e.value


def test_the_route_gives_the_duplicate_its_code_and_the_card(monkeypatch):
    e = _route(monkeypatch, {"error": "duplicate_box", "box_id": "9568129-1", "job_card_id": 11111111,
                             "job_card_number": "PLAN-9-L1-S1", "same_card": False})
    assert e.status_code == 409
    assert e.detail["error"] == "duplicate_box"
    assert e.detail["job_card_number"] == "PLAN-9-L1-S1" and e.detail["same_card"] is False
    assert e.detail["message"].startswith("Box 9568129-1 is already recorded on job card PLAN-9-L1-S1.")


def test_an_ambiguous_box_is_not_called_a_duplicate(monkeypatch):
    e = _route(monkeypatch, {"error": "ambiguous_box", "code": "48213307",
                             "tables": ["po_box", "cold_storage_box"]})
    assert e.status_code == 409
    assert e.detail["error"] == "ambiguous_box"
    assert "duplicate" not in e.detail["message"].lower()
    assert "po_box, cold_storage_box" in e.detail["message"]


# ── close refused for a missing sign-off ──────────────────────────────────────

def test_the_missing_sign_off_is_named_in_words():
    assert jcv2.missing_sign_offs_message(["production_head"]) == (
        "Production Head must sign off before this job card can be closed. "
        "Sign it on the Sign-offs tab, then close.")
    assert jcv2.missing_sign_offs_message(["production_head", "qc_inspector"]).startswith(
        "Production Head and QC Inspector must sign off")
    assert jcv2.missing_sign_offs_message(["shift_lead"]).startswith("shift lead must sign off")


class TxConn:
    def transaction(self):
        return _Ctx(None)


def test_the_close_route_sends_the_code_and_the_words(monkeypatch):
    async def refuse(conn, **kw):
        return {"error": "missing_sign_offs", "missing": ["production_head"]}

    async def in_scope(conn, **kw):
        return None

    monkeypatch.setattr(jcv2, "close_job_card", refuse)
    monkeypatch.setattr(PR, "_assert_jc_writable_by_user", in_scope)
    user = SimpleNamespace(full_name="Floor Fay", phone=None, is_admin=False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(PR.close_job_card_v2(_request(TxConn()), JC, SimpleNamespace(add_task=None),
                                         None, user=user))
    assert e.value.status_code == 400
    assert e.value.detail["error"] == "missing_sign_offs"
    assert e.value.detail["missing"] == ["production_head"]
    assert e.value.detail["message"].startswith("Production Head must sign off")
