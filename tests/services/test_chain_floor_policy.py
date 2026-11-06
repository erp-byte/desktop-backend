"""Job-card chain floors: planners plan any floor of their factory, others only
their own, and only the floors a change touches are checked (decided 2026-10-27).

Before: the pop-up offered every floor of the factory, while each chain route
refused any floor in the request outside the user's grants — so 8 floor-scoped
planners could not card a chain that ran onto a packing floor, a quantity-only
live edit was refused over floors it did not change, and the floors of the cards
an edit removed or a replace deleted were never checked. No route checked the
plan's factory.

Now (services/chain_floors + router._chain_floor_gate):
  * the plan's factory must be the user's;
  * a floor must be a production floor of that factory, stored in the declared
    spelling (a floor a row already had keeps its stored value);
  * the touched floors — added, moved (old and new), removed — must be the
    user's unless they may approve plans, have no floor grants, or are admin;
  * every refusal comes before the first write, inside the transaction.
Services are faked or scripted; no database.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.modules.production import router as R
from app.modules.production.services import chain_floors as cf
from app.modules.production.services import job_card_v2 as jcv2
from tests.services.test_bom_changes_pdf_rebuild import ScriptConn, card

LINE = 5101


def _user(*, floors=("Roasting Area",), warehouses=("A185",), admin=False):
    return SimpleNamespace(is_admin=admin, allowed_floors=list(floors), allowed_warehouses=list(warehouses),
                           role_ids=[6], full_name="Team Leader", email=None, phone="98", user_id=44)


@pytest.fixture
def approves(monkeypatch):
    """Whether the user holds plans > approve: a planner (True) or a team leader /
    floor manager (False). Records each permission asked."""
    asked = []
    state = {"may": False}

    async def check(conn, role_ids, is_admin, module, sub, subsub, action, **kw):
        asked.append((module, sub, subsub, action))
        return state["may"]
    monkeypatch.setattr(R, "check_permission", check)
    state["asked"] = asked
    return state


class _Tx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return None

    async def __aexit__(self, exc_type, *rest):
        self.conn.tx_exits.append(exc_type)
        return False


class _Conn:
    """The route's own reads: the line's plan factory. Records how the
    transaction ended."""

    def __init__(self, factory="A-185"):
        self.factory = factory
        self.tx_exits: list = []

    def transaction(self):
        return _Tx(self)

    async def fetchval(self, sql, *args):
        assert sql == R._LINE_FACTORY_SQL, sql
        return self.factory

    async def fetch(self, sql, *args):
        return []


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


@pytest.fixture
def services(monkeypatch):
    """Fakes for everything the create route writes through; `calls` lists them."""
    calls = []

    async def merge(conn, line, ids):
        calls.append(("consolidate", line)); return None

    async def create(conn, line, **kw):
        calls.append(("create", kw)); return {"count": 2, "job_card_ids": [], "plan_id": 77}

    async def release(conn, plan_id, **kw):
        return None
    monkeypatch.setattr(jcv2, "consolidate_plan_lines_for_merge", merge)
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)
    monkeypatch.setattr(jcv2, "maybe_release_plan_from_jcs", release)
    return calls


# Roasting on the user's own floor, packing on someone else's — 121 of 281 recent
# chains crossed floors like this.
CROSS_FLOOR = R.JobCardLineCreate(
    qty_kg=100, wip_steps=[R.JobCardLineCreateStep(process="Roasting", floor="roasting area")],
    pkg_floor="FFS Packing Area")


def _create(conn, user, body=CROSS_FLOOR):
    return asyncio.run(R.create_line_job_cards_v2(_req(conn), LINE, body, BackgroundTasks(), user=user))


# ── create ─────────────────────────────────────────────────────────────────

def test_a_planner_with_some_floors_cards_a_chain_across_their_factory(approves, services):
    approves["may"] = True
    conn = _Conn()
    out = _create(conn, _user())
    assert out["count"] == 2
    created = dict(services)["create"]
    # Stored in the declared spelling, not as typed.
    assert [s["floor"] for s in created["wip_steps"]] == ["Roasting Area"]
    assert created["pkg_floor"] == "FFS Packing Area"
    assert ("production", "plans", "approve", "create") in approves["asked"]
    assert conn.tx_exits[0] is None


def test_a_team_leader_is_refused_a_floor_outside_their_grants(approves, services):
    conn = _Conn()
    with pytest.raises(HTTPException) as exc:
        _create(conn, _user())
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert detail["error"] == "floor_not_allowed"
    assert detail["details"] == {"requested": ["FFS Packing Area"], "allowed_floors": ["Roasting Area"]}
    assert detail["message"].startswith("FFS Packing Area is not one of your floors")
    assert "Your floors in A-185: Roasting Area." in detail["message"]
    assert services == []                               # refused before the merge or a card
    assert conn.tx_exits[0] is not None


def test_a_team_leader_on_their_own_floors_passes(approves, services):
    body = R.JobCardLineCreate(qty_kg=100, wip_steps=[], pkg_floor="Roasting Area")
    _create(_Conn(), _user(), body)
    assert [c[0] for c in services] == ["consolidate", "create"]


def test_no_grants_or_admin_are_not_limited(approves, services):
    _create(_Conn(), _user(floors=()))
    _create(_Conn(), _user(admin=True, warehouses=()))
    assert [c[0] for c in services] == ["consolidate", "create"] * 2


def test_a_plan_outside_the_users_factory_is_refused(approves, services):
    approves["may"] = True
    conn = _Conn(factory="A-185")
    with pytest.raises(HTTPException) as exc:
        _create(conn, _user(warehouses=("W202",)))
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "outside_factory"
    assert "This plan is in A-185, which is not one of your factories (W202)" in exc.value.detail["message"]
    assert services == []
    assert conn.tx_exits[0] is not None


def test_a_floor_of_another_factory_is_a_400(approves, services):
    approves["may"] = True
    body = R.JobCardLineCreate(qty_kg=100, wip_steps=[R.JobCardLineCreateStep(process="Roasting", floor="Terrace")],
                               pkg_floor="FG store")
    with pytest.raises(HTTPException) as exc:
        _create(_Conn(), _user(), body)
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "floor_not_in_factory"
    assert "Terrace is not a production floor of A-185" in exc.value.detail["message"]
    assert services == []


# ── live edit ──────────────────────────────────────────────────────────────

class _LiveConn(ScriptConn):
    """The real apply_live_job_card_edits against scripted SQL, plus the route's
    factory read. `writes` are the statements that change data."""

    def __init__(self, cards, factory="A-185"):
        super().__init__(cards, heads=[cards[0]["job_card_id"]])
        self.factory = factory
        self.tx_exits: list = []

    def transaction(self):
        return _Tx(self)

    async def fetchval(self, sql, *a):
        if sql == R._LINE_FACTORY_SQL:
            return self.factory
        return await super().fetchval(sql, *a)

    @property
    def writes(self):
        return [s for s, _ in self.log if s.split()[0] in ("UPDATE", "INSERT", "DELETE")]


# Roasting (running, the team leader's floor) -> Sorting (someone else's, stored in
# lower case) -> packing on FG store (someone else's).
def _chain():
    return [card(1, 1, "in_progress", floor="Roasting Area"),
            card(2, 2, "locked", floor="sorting area"),        # stored lowercase on RDS
            card(3, 3, "locked", floor="FG store", process_name="Packaging", stage="packaging")]


@pytest.fixture
def so_sync(monkeypatch):
    synced = []

    async def sync(conn, plan_line_id, **kw):
        synced.append(kw["delta_kg"]); return {"synced": True}
    monkeypatch.setattr(jcv2, "sync_so_from_qty_delta", sync)
    return synced


def _live(conn, user, steps, *, qty_kg=100, pkg_floor="FG store", reasons=None):
    body = R.JobCardLineApplyEdits(qty_kg=qty_kg, steps=[R.JobCardLineEditStep(**s) for s in steps],
                                   pkg_floor=pkg_floor, remove_reasons=reasons)
    return asyncio.run(R.apply_line_job_card_edits_v2(_req(conn), LINE, body, BackgroundTasks(), user=user))


def test_a_quantity_only_live_edit_checks_no_floor(approves, so_sync):
    """Every floor is sent back as it is (one in other case): nothing touched, so
    the team leader may change the quantity of a chain that runs onto other floors."""
    conn = _LiveConn(_chain())
    out = _live(conn, _user(), [{"job_card_id": 1, "process": "P1", "floor": "Roasting Area"},
                                {"job_card_id": 2, "process": "P2", "floor": "Sorting Area"}],
                qty_kg=120)
    assert out["qty_changed"] is True and out["floors_changed"] == 0
    assert so_sync == [20.0]
    # The oddly spelt card kept its stored floor.
    relinked = [a for s, a in conn.log if s.startswith("UPDATE job_card_v2 SET step_number") and a[-1] == 2]
    assert relinked and relinked[0][3] == "sorting area"


def test_a_team_leader_removing_a_card_on_another_floor_is_refused_before_any_write(approves, so_sync):
    conn = _LiveConn(_chain())
    with pytest.raises(HTTPException) as exc:
        _live(conn, _user(), [{"job_card_id": 1, "process": "P1", "floor": "Roasting Area"}],
              qty_kg=120, reasons={"2": "not needed"})
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "floor_not_allowed"
    assert exc.value.detail["details"]["requested"] == ["sorting area"]
    assert conn.writes == [] and so_sync == []         # not even the qty change
    assert conn.tx_exits[0] is not None


def test_moving_a_card_checks_the_floor_it_leaves(approves, so_sync):
    """Moving card 2 off someone else's floor onto the team leader's own still
    touches the floor it leaves."""
    conn = _LiveConn(_chain())
    with pytest.raises(HTTPException) as exc:
        _live(conn, _user(), [{"job_card_id": 1, "process": "P1", "floor": "Roasting Area"},
                              {"job_card_id": 2, "process": "P2", "floor": "Roasting Area"}])
    assert exc.value.detail["details"]["requested"] == ["sorting area"]
    assert conn.writes == []


def test_a_planner_may_move_and_remove_across_the_factory(approves, so_sync, monkeypatch):
    async def cancel(conn, *, job_card_id, reason, deleted_by):
        return {"removed": True, "snapshot": {}}
    monkeypatch.setattr(jcv2, "_force_record_and_cancel_jc", cancel)
    approves["may"] = True
    conn = _LiveConn(_chain())
    out = _live(conn, _user(), [{"job_card_id": 1, "process": "P1", "floor": "mezzanine"}],
                pkg_floor="FFS Packing Area")
    assert out["removed"] == 1 and out["floors_changed"] == 2
    moved = {a[-1]: a[3] for s, a in conn.log if s.startswith("UPDATE job_card_v2 SET step_number")}
    assert moved == {1: "Mezzanine", 3: "FFS Packing Area"}


def test_a_live_edit_outside_the_users_factory_is_refused(approves, so_sync):
    conn = _LiveConn(_chain(), factory="W-202")
    with pytest.raises(HTTPException) as exc:
        _live(conn, _user(), [{"job_card_id": 1, "process": "P1", "floor": "Roasting Area"},
                              {"job_card_id": 2, "process": "P2", "floor": "sorting area"}], qty_kg=120)
    assert exc.value.detail["error"] == "outside_factory"
    assert conn.log == [] and so_sync == []             # refused before the service ran


# ── replace (un-started edit) ──────────────────────────────────────────────

def test_replace_checks_only_what_the_rebuild_changes(approves, monkeypatch):
    created = []

    async def create(conn, line, **kw):
        created.append(kw); return {"job_card_ids": [900, 901]}
    monkeypatch.setattr(jcv2, "create_job_cards_for_line", create)
    chain = [card(21, 1, "unlocked", floor="Roasting Area"), card(22, 2, "locked", floor="FG store")]

    # Insert a step on the team leader's own floor in front: FG store is kept.
    conn = _LiveConn(chain)
    body = R.JobCardLineCreate(qty_kg=100, pkg_floor="fg store", wip_steps=[
        R.JobCardLineCreateStep(process="Cleaning", floor="Roasting Area"),
        R.JobCardLineCreateStep(process="Roasting", floor="Roasting Area")])
    asyncio.run(R.replace_line_job_cards_v2(_req(conn), LINE, body, BackgroundTasks(), user=_user()))
    assert created[0]["pkg_floor"] == "FG store"

    # Moving packing to another floor touches FG store and FFS Packing Area: refused before the delete.
    conn = _LiveConn(chain)
    body = R.JobCardLineCreate(qty_kg=100, pkg_floor="FFS Packing Area",
                               wip_steps=[R.JobCardLineCreateStep(process="Roasting", floor="Roasting Area")])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.replace_line_job_cards_v2(_req(conn), LINE, body, BackgroundTasks(), user=_user()))
    assert exc.value.detail["details"]["requested"] == ["FFS Packing Area", "FG store"]
    assert conn.writes == [] and len(created) == 1


# ── plan steps and the single-card PATCH ───────────────────────────────────

class _StepConn(_Conn):
    def __init__(self, row):
        super().__init__()
        self.row, self.wrote = row, []

    async def fetchrow(self, sql, *args):
        if sql == R._STEP_FACTORY_SQL or "FROM job_card_v2 WHERE job_card_id = $1" in " ".join(sql.split()):
            return self.row
        raise AssertionError(sql[:80])


def test_adding_a_step_follows_the_same_rule(approves, monkeypatch):
    from app.modules.production.services import plan_v2
    added = []

    async def add(conn, plan_line_id, data):
        added.append(data["floor"]); return {"added": True, "step": {}}
    monkeypatch.setattr(plan_v2, "add_step", add)
    step = lambda floor: R.StepV2Add(process_name="Sorting", floor=floor)   # noqa: E731
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.add_step_v2(_req(_Conn()), LINE, step("Sorting Area"), BackgroundTasks(), user=_user()))
    assert exc.value.status_code == 403 and added == []
    approves["may"] = True
    asyncio.run(R.add_step_v2(_req(_Conn()), LINE, step("sorting area"), BackgroundTasks(), user=_user()))
    assert added == ["Sorting Area"]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.add_step_v2(_req(_Conn()), LINE, step("Sorting Area"), BackgroundTasks(),
                                  user=_user(warehouses=("W202",))))
    assert exc.value.detail["error"] == "outside_factory"


def test_moving_a_carded_step_checks_the_floor_its_cards_leave(approves, monkeypatch):
    from app.modules.production.services import plan_v2
    wrote = []

    async def update(conn, step_id, fields):
        wrote.append(fields); return {"updated": True}
    monkeypatch.setattr(plan_v2, "update_step", update)
    step = {"floor": "Sorting Area", "warehouse": "A-185", "card_floors": ["Sorting Area"]}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(R.update_step_v2(_req(_StepConn(step)), 61, R.StepV2Patch(floor="Roasting Area"),
                                     user=_user()))
    assert exc.value.detail["details"]["requested"] == ["Sorting Area"]
    # The same move on a step with no cards to move touches only the new floor.
    asyncio.run(R.update_step_v2(_req(_StepConn({**step, "card_floors": []})), 61,
                                 R.StepV2Patch(floor="roasting area"), user=_user()))
    assert wrote == [{"floor": "Roasting Area"}]


def test_patching_a_cards_floor_checks_both_floors_and_the_factory(approves, monkeypatch):
    patched = []

    async def patch(conn, *, job_card_id, fields, updated_by=None):
        patched.append(fields); return {"updated": True}
    monkeypatch.setattr(jcv2, "patch_job_card", patch)
    run = lambda row, floor, user: asyncio.run(R.patch_jc_v2(   # noqa: E731
        _req(_StepConn(row)), 41, R.PatchJobCardV2Request(floor=floor), user=user))

    with pytest.raises(HTTPException) as exc:
        run({"factory": "A185", "floor": "Roasting Area"}, "Sorting Area", _user())
    assert exc.value.detail["details"]["requested"] == ["Sorting Area"]
    with pytest.raises(HTTPException) as exc:
        run({"factory": "W202", "floor": "Terrace"}, "Terrace", _user())
    assert exc.value.detail["error"] == "outside_factory"
    assert "This job card is in W202" in exc.value.detail["message"]
    run({"factory": "A185", "floor": "roasting area"}, "Roasting Area", _user())   # unchanged: kept
    assert patched == [{"floor": "roasting area"}]


# ── the rule itself ────────────────────────────────────────────────────────

def test_touched_floors_are_a_multiset_difference_ignoring_case():
    assert cf.touched_floors(["Roasting Area", "FG store"],
                             ["Sorting Area", "roasting area", "FG store"]) == ["Sorting Area"]
    assert cf.touched_floors(["A", "B"], ["B", "A"]) == []
    assert cf.touched_floors(["A", "A"], ["A"]) == ["A"]
    assert cf.touched_floors(["A"], []) == ["A"]


def test_floors_are_spelt_as_declared_or_as_stored():
    assert cf.spell_floor("A-185", " fg STORE ") == "FG store"
    assert cf.spell_floor("A-185", "roasting area", keep=["roasting area"]) == "roasting area"
    assert cf.spell_floor("A-185", "Terrace") is None
    assert cf.spell_floor("A-185", "A185 Cold") is None          # a store, not a production stage
    assert cf.spell_floor("F53", "Bay 2") == "Bay 2"            # no declared floors: as typed
    assert cf.spell_floor("A-185", "  ") == ""
    assert "Store" not in cf.production_floors("W-202") and "Terrace" in cf.production_floors("W202")


def test_the_refusal_lists_the_users_floors_in_this_factory():
    user = _user(floors=("roasting area", "Terrace"))
    out = cf.floor_refusal(user, "A-185", ["Sorting Area", "sorting area", "Roasting Area"])
    assert out["details"] == {"requested": ["Sorting Area"], "allowed_floors": ["Roasting Area"]}
    none_here = cf.floor_refusal(_user(floors=("Terrace",)), "A-185", ["Sorting Area"])
    assert "None of your floors are in A-185" in none_here["message"]
    assert cf.floor_refusal(user, "A-185", ["Sorting Area"], any_floor_in_factory=True) is None
