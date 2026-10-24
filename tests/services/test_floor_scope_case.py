"""Floor checks ignore case and outer spaces; floor grants are checked when saved.

Before (audit 2026-10-23): about a dozen job-card checks compared a card's floor
with the user's grants letter for letter, while the screens and the WhatsApp
notices ignored case. A card on "roasting area" (4 on RDS) was closed to a floor
manager granted "Roasting Area" — the notice reached them, its link opened
"access denied" — and grants were saved exactly as typed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.warehouse_scope import (
    FLOOR_NORM_SQL, floor_scope_values, normalise_floor, user_has_floor,
)
from app.modules.auth.services import floor_grants
from app.modules.production import router as R
from app.modules.production.services import jc_annexures_v2, job_card_v2 as jcv2


def test_the_rule():
    assert normalise_floor("  roasting area ") == "ROASTING AREA"
    assert user_has_floor(["Roasting Area"], "roasting area")
    assert user_has_floor(["roasting area "], "Roasting Area")
    assert not user_has_floor(["Roasting Area"], "Sorting Area")
    assert not user_has_floor([], "Roasting Area")
    assert not user_has_floor(["Roasting Area"], None)
    assert floor_scope_values(["Terrace", " terrace", "", None]) == ["TERRACE"]
    assert FLOOR_NORM_SQL("floor") == "upper(btrim(coalesce(floor, '')))"


def test_it_is_the_notices_rule():
    """Access and the notices must agree, or a notice links to a 403."""
    from app.modules.production.services.wa_notify import covers_place
    assert covers_place(["W202"], ["Roasting Area"], "W-202", "roasting area")
    assert user_has_floor(["Roasting Area"], "roasting area")


# ── the routes that read one card ──────────────────────────────────────────

FLOOR_MANAGER = SimpleNamespace(is_admin=False, allowed_warehouses=["A185"],
                                allowed_floors=["Roasting Area"], role_name="floor_manager")


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


def _req(conn=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn))))


@pytest.mark.parametrize("floor, opens", [("roasting area", True), (" Roasting Area", True),
                                          ("Sorting Area", False)])
def test_the_card_detail_follows_the_rule(monkeypatch, floor, opens):
    async def card(conn, job_card_id):
        return {"job_card_id": job_card_id, "factory": "A-185", "floor": floor}
    monkeypatch.setattr(jcv2, "get_job_card", card)
    run = R.get_job_card_v2(_req(object()), 41, user=FLOOR_MANAGER)
    if opens:
        assert asyncio.run(run)["floor"] == floor
    else:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(run)
        assert exc.value.status_code == 403


class _CardConn:
    def __init__(self, floor): self.floor = floor

    async def fetchrow(self, sql, *args):
        return {"factory": "A185", "floor": self.floor}


@pytest.mark.asyncio
async def test_writes_on_a_card_follow_the_rule():
    ok = await jc_annexures_v2.assert_jc_in_scope(
        _CardConn("roasting area"), job_card_id=41,
        allowed_warehouses=["A185"], allowed_floors=["Roasting Area"])
    assert ok == {"ok": True}
    out = await jc_annexures_v2.assert_jc_in_scope(
        _CardConn("Sorting Area"), job_card_id=41,
        allowed_warehouses=["A185"], allowed_floors=["Roasting Area"])
    assert out["error"] == "out_of_scope"


class _ListConn:
    def __init__(self): self.sql, self.args = [], []

    async def fetch(self, sql, *args):
        self.sql.append(sql); self.args.append(args)
        return []

    async def fetchval(self, sql, *args):
        self.sql.append(sql); self.args.append(args)
        return 0

    async def fetchrow(self, sql, *args):
        self.sql.append(sql); self.args.append(args)
        return {"total": 0}


@pytest.mark.asyncio
async def test_the_card_list_compares_floors_the_same_way():
    conn = _ListConn()
    try:
        await jcv2.list_job_cards(conn, user_scope_floors=["Roasting Area", "roasting area "])
    except Exception:  # noqa: BLE001 — only the filter it built matters here
        pass
    where = next(s for s in conn.sql if "upper(btrim(coalesce(floor, '')))" in s)
    assert "= ANY(" in where
    assert ["ROASTING AREA"] in [list(a) for args in conn.args for a in args if isinstance(a, list)]


# ── grants are checked when saved ──────────────────────────────────────────

def test_a_grant_is_saved_in_the_declared_spelling():
    out, unknown = floor_grants.canonical_floor_grants(["roasting area", " TERRACE ", "Terrace"])
    assert out == ["Roasting Area", "Terrace"] and unknown == []


def test_a_cold_store_floor_recorded_by_stock_take_is_kept():
    out, unknown = floor_grants.canonical_floor_grants(["rishi"], recorded=["RISHI"])
    assert out == ["RISHI"] and unknown == []


def test_a_floor_the_user_already_holds_is_kept():
    out, unknown = floor_grants.canonical_floor_grants(["Savla Bond"], held=["Savla Bond"])
    assert out == ["Savla Bond"] and unknown == []


def test_an_unknown_floor_is_named():
    out, unknown = floor_grants.canonical_floor_grants(["Roastng Area", "Terrace"])
    assert out == ["Terrace"] and unknown == ["Roastng Area"]


class _GrantConn:
    async def fetchval(self, sql, *args):
        assert "allowed_floors FROM auth_user" in sql
        return ["Savla"]


@pytest.mark.asyncio
async def test_saving_an_unknown_floor_is_a_400(monkeypatch):
    async def recorded(conn): return ["ESKIMO"]
    monkeypatch.setattr(floor_grants, "recorded_floors", recorded)
    assert await floor_grants.checked_floor_grants(
        _GrantConn(), ["first floor", "eskimo", "Savla"], user_id=7) == ["First Floor", "ESKIMO", "Savla"]
    with pytest.raises(HTTPException) as exc:
        await floor_grants.checked_floor_grants(_GrantConn(), ["Frist Floor"], user_id=7)
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "unknown_floor"
    assert "Frist Floor" in exc.value.detail["message"]
