"""Who approved / signed / force-unlocked is the logged-in user (audit 2026-10-23).

Before:
  * approve saved the name the browser sent — `full_name || phone || "user"` —
    which is how "user" approved 54 plans; approving again overwrote the first
    approver;
  * carding every article approved the plan for anyone allowed to card it (floor
    managers, team leaders), without plans › approve, and nobody was told;
  * the Production Head sign-off saved a typed name under any role the request
    named; force-unlock saved a typed "authority".
Agreed 2026-10-24: auto-approve only for those who may approve; the Production
Head sign-off is given by floor managers and admins.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.modules.production import router as R
from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services import plan_v2


def _user(name="Asha P", *, admin=False, roles=(), floors=(), warehouses=()):
    return SimpleNamespace(is_admin=admin, full_name=name, email=None, phone="98", user_id=21,
                           role_name=(roles[0] if roles else None), role_ids=[5],
                           held=set(roles), allowed_entities=[], allowed_warehouses=list(warehouses),
                           allowed_floors=list(floors))


class _Conn:
    """Answers the role check from the user's `held` roles, records the rest."""

    def __init__(self, user=None, *, card_floor="Roasting Area"):
        self.user = user
        self.card_floor = card_floor
        self.tx_exits: list = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self): return None

            async def __aexit__(self, exc_type, *rest):
                conn.tx_exits.append(exc_type)
                return False
        return _Tx()

    async def fetchval(self, sql, *args):
        if "FROM auth_role" in sql:           # holds_role
            return bool(self.user.held & set(args[1]))
        raise AssertionError(sql[:60])

    async def fetchrow(self, sql, *args):
        if "SELECT entity, factory, floor FROM job_card_v2" in sql:
            return {"entity": "cfpl", "factory": "A185", "floor": self.card_floor}
        raise AssertionError(sql[:60])

    async def fetch(self, sql, *args):
        return [{"plan_id": 77}]


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn))))


# ── approve ────────────────────────────────────────────────────────────────

def test_approve_records_the_login_not_what_the_browser_sent(monkeypatch):
    seen = []

    async def approve(conn, plan_id, approved_by):
        seen.append(approved_by)
        return {"approved": True, "job_cards": {"lines": []}}
    monkeypatch.setattr(plan_v2, "approve_plan", approve)
    asyncio.run(R.approve_plan_v2(_req(_Conn()), 9, R.PlanV2Approve(approved_by="user"),
                                  BackgroundTasks(), user=_user("Harsh Arora", roles=("planner",))))
    assert seen == ["Harsh Arora"]


def test_the_browser_need_not_send_a_name():
    assert R.PlanV2Approve().approved_by == ""


class _ApproveConn:
    def __init__(self): self.update = None

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT status FROM production_plan_v2"):
            return {"status": "approved"}
        if s.startswith("UPDATE production_plan_v2"):
            self.update = s
            return {"plan_id": 9, "status": "approved"}
        raise AssertionError(s[:60])

    async def fetch(self, sql, *args):
        return []                                       # no problems


@pytest.mark.asyncio
async def test_approving_again_keeps_the_first_approver(monkeypatch):
    async def fan_out(conn, plan_id): return {"lines": []}
    monkeypatch.setattr(jcv2, "create_job_cards_from_plan", fan_out)
    conn = _ApproveConn()
    await plan_v2.approve_plan(conn, 9, "Someone Later")
    assert "approved_by = COALESCE(approved_by, $2)" in conn.update
    assert "approved_at = COALESCE(approved_at, NOW())" in conn.update


# ── carding every article ─────────────────────────────────────────────────

class _ReleaseConn:
    def __init__(self, carded=2, lines=2):
        self.carded, self.lines, self.approved = carded, lines, False

    async def fetchrow(self, sql, *args):
        if "SELECT status FROM production_plan_v2" in sql:
            return {"status": "draft"}
        return {"lines": self.lines, "carded": self.carded}

    async def execute(self, sql, *args):
        self.approved = True


@pytest.mark.asyncio
async def test_a_floor_manager_carding_the_last_article_leaves_the_plan_for_a_planner():
    conn = _ReleaseConn()
    out = await jcv2.maybe_release_plan_from_jcs(conn, 77, "Ravi K", may_approve=False)
    assert out == "awaits_approval" and conn.approved is False


@pytest.mark.asyncio
async def test_a_planner_carding_the_last_article_approves_it():
    conn = _ReleaseConn()
    assert await jcv2.maybe_release_plan_from_jcs(conn, 77, "Asha P", may_approve=True) == "approved"
    assert conn.approved is True


@pytest.mark.asyncio
async def test_a_partly_carded_plan_says_nothing():
    assert await jcv2.maybe_release_plan_from_jcs(_ReleaseConn(carded=1), 77, may_approve=False) is None


def _create(conn, user):
    body = R.JobCardLineCreate(qty_kg=100, wip_steps=[], pkg_floor="FG store")
    return asyncio.run(R.create_line_job_cards_v2(_req(conn), 5101, body, BackgroundTasks(), user=user))


@pytest.mark.parametrize("may, outcome", [(False, "awaits_approval"), (True, "approved")])
def test_the_create_route_asks_whether_the_user_may_approve(monkeypatch, may, outcome):
    seen = {}

    async def no_merge(conn, line, ids): return None
    async def create(conn, line, **kw): return {"count": 1, "job_card_ids": [], "plan_id": 77}
    async def release(conn, plan_id, approved_by=None, *, may_approve=True):
        seen.update(by=approved_by, may=may_approve)
        return "approved" if may_approve else "awaits_approval"
    async def check(conn, role_ids, is_admin, module, sub, subsub, action, **kw):
        seen["perm"] = (module, sub, subsub, action)
        return may
    monkeypatch.setattr(jcv2, "consolidate_plan_lines_for_merge", no_merge)
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)
    monkeypatch.setattr(jcv2, "maybe_release_plan_from_jcs", release)
    monkeypatch.setattr(R, "check_permission", check)

    out = _create(_Conn(), _user("Ravi K", roles=("floor_manager",)))
    assert out["plan_release"] == outcome
    assert seen == {"by": "Ravi K", "may": may, "perm": ("production", "plans", "approve", "create")}


# ── sign-off ───────────────────────────────────────────────────────────────

@pytest.fixture
def signed(monkeypatch):
    rows = []

    async def add(conn, *, job_card_id, role, signed_by, notes=None):
        rows.append((role, signed_by))
        return {"signed": True}
    monkeypatch.setattr(jcv2, "add_sign_off", add)
    return rows


def _sign(user, role="production_head", typed="Production Head Sir", conn=None):
    body = R.SignOffRequest(role=role, signed_by_name=typed)
    return asyncio.run(R.sign_off_v2(_req(conn or _Conn(user)), 41, body, user=user))


def test_a_floor_manager_signs_as_themselves_whatever_is_typed(signed):
    _sign(_user("Ravi K", roles=("floor_manager",)))
    assert signed == [("production_head", "Ravi K")]


def test_an_admin_may_sign(signed):
    _sign(_user("Admin", admin=True))
    assert signed == [("production_head", "Admin")]


@pytest.mark.parametrize("role", ["team_leader", "planner", "qc_inspector"])
def test_others_cannot_give_the_production_head_sign_off(signed, role):
    with pytest.raises(HTTPException) as exc:
        _sign(_user("Someone", roles=(role,)))
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "sign_off_role_required"
    assert "floor manager or an admin" in exc.value.detail["message"]
    assert signed == []


def test_an_unknown_sign_off_role_is_refused(signed):
    with pytest.raises(HTTPException) as exc:
        _sign(_user("Ravi K", roles=("floor_manager",)), role="plant_manager")
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "unknown_sign_off_role"


def test_qc_counts_every_role_not_just_the_first(signed):
    """A qc_manager whose primary role is something else used to be refused."""
    user = _user("Priya S", roles=("stock_take", "qc_manager"), floors=["roasting area"])
    _sign(user, role="qc_inspector", conn=_Conn(user, card_floor="Roasting Area"))
    assert signed == [("qc_inspector", "Priya S")]


def test_qc_outside_their_floor_is_refused(signed):
    user = _user("Priya S", roles=("qc_manager",), floors=["Terrace"])
    with pytest.raises(HTTPException) as exc:
        _sign(user, role="qc_inspector", conn=_Conn(user, card_floor="Roasting Area"))
    assert exc.value.detail["error"] == "qc_scope_mismatch"


# ── force-unlock ───────────────────────────────────────────────────────────

class _UnlockConn(_Conn):
    async def fetchrow(self, sql, *args):
        return {"factory": "A185", "floor": "Roasting Area"}


def test_force_unlock_records_the_login(monkeypatch):
    seen = {}

    async def unlock(conn, *, job_card_id, authority, reason):
        seen.update(by=authority, reason=reason)
        return {"error": "not_locked", "message": "JC is already unlocked; nothing to force"}
    monkeypatch.setattr(jcv2, "force_unlock", unlock)
    user = _user("Ravi K", roles=("floor_manager",), floors=["Roasting Area"], warehouses=["A185"])
    body = R.ForceUnlockV2Request(authority="Plant Manager", reason="Upstream done on paper")
    with pytest.raises(HTTPException):
        asyncio.run(R.force_unlock_v2(_req(_UnlockConn(user)), 41, body, BackgroundTasks(), user=user))
    assert seen == {"by": "Ravi K", "reason": "Upstream done on paper"}


def test_force_unlock_needs_no_typed_name():
    assert R.ForceUnlockV2Request(reason="x").authority is None
