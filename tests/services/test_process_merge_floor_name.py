"""Merge process: the group's floor keeps the stored spelling.

Regression: validate_process_merge grouped lines by lower(trim(first floor)) and
returned that lowercased value as the group's floor ("roasting area"). The Merge
process wizard pre-fills every product's packaging floor (and any blank process
floor) from it, and POST /plans-v2/merge-process checks floors against the
user's allowed_floors with an exact match — so a non-admin assigned to
"Roasting Area" was refused ("User is not assigned to floor(s): roasting area"),
and an admin's merge wrote the lowercased name onto the job cards.

Grouping stays case-insensitive; the floor sent back is the stored one.
"""
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.modules.production import router as R
from app.modules.production.services.plan_v2 import validate_process_merge


def _line(plan_line_id, floor_stored):
    # What the lines query returns: floor1 is lower(trim(first step's floor)).
    return {
        "plan_line_id": plan_line_id, "plan_id": 900 + plan_line_id,
        "fg_sku_name": f"FG {plan_line_id}", "bom_id": 70 + plan_line_id,
        "planned_qty_kg": 100, "planned_qty_units": None,
        "plan_status": "approved",
        "factory": "a-185", "entity": "cfpl",
        "floor1": floor_stored.strip().lower(),
        "rm_fingerprint": "sunflower seeds",
        "jc_count": 0, "jc_started": 0, "has_cards": False,
    }


def _step(plan_line_id, order, process, floor):
    # The steps query returns floors as stored.
    return {"plan_line_id": plan_line_id, "step_order": order,
            "process_name": process, "stage": process.lower(), "floor": floor}


class FakeConn:
    def __init__(self, lines, steps):
        self.lines, self.steps = lines, steps

    async def fetch(self, sql, *args):
        if "FROM production_plan_step_v2" in sql and "FROM production_plan_line_v2" not in sql:
            return self.steps
        if "FROM bom_process_route" in sql:      # the groups' suggested SFG: no routing
            return []
        return self.lines

    async def fetchrow(self, sql, *args):
        assert "FROM sfg_canonical_map" in sql, sql   # nor a standard SFG
        return None


@pytest.mark.asyncio
async def test_group_floor_is_the_stored_floor_name():
    conn = FakeConn(
        lines=[_line(1, "Roasting Area"), _line(2, "Roasting Area")],
        steps=[
            _step(1, 1, "Roasting", "Roasting Area"), _step(1, 2, "Packing", "FG store"),
            _step(2, 1, "Roasting", "Roasting Area"), _step(2, 2, "Packing", "FG store"),
        ],
    )
    out = await validate_process_merge(conn, [901, 902])
    assert len(out["groups"]) == 1
    assert out["groups"][0]["key"]["floor"] == "Roasting Area"


@pytest.mark.asyncio
async def test_spelling_differences_still_group_together():
    """Grouping ignores case and surrounding spaces; the first product's
    stored spelling (trimmed) is the one sent back."""
    conn = FakeConn(
        lines=[_line(1, " Roasting Area "), _line(2, "roasting area")],
        steps=[
            _step(1, 1, "Roasting", " Roasting Area "),
            _step(2, 1, "Roasting", "roasting area"),
        ],
    )
    out = await validate_process_merge(conn, [901, 902])
    assert len(out["groups"]) == 1
    assert {m["plan_line_id"] for m in out["groups"][0]["members"]} == {1, 2}
    assert out["groups"][0]["key"]["floor"] == "Roasting Area"


# The merge endpoint's floor check, for a non-admin floor manager assigned to one
# floor (no plans > approve, so only their own floors: decided 2026-10-27). The
# service runs for real up to its first write, the DELETE of the members' cards.
FLOOR_USER = SimpleNamespace(is_admin=False, allowed_floors=["Roasting Area"], allowed_warehouses=["A185"],
                             role_ids=[4], full_name="Floor Manager", email="", phone="", user_id=31)


class _ReachedWrite(Exception):
    """Raised by the fake connection: the merge got past every check to its first write."""


def _member(plan_line_id):
    return {"plan_line_id": plan_line_id, "plan_id": 900, "bom_id": 70 + plan_line_id,
            "fg_sku_name": f"FG {plan_line_id}", "customer_name": None, "planned_qty_kg": 100,
            "planned_qty_units": None, "factory": "a-185", "entity": "cfpl", "entity_raw": "cfpl", "warehouse_raw": "A-185",
            "plan_status": "approved", "floor1": "roasting area", "rm_fp": "sunflower seeds",
            "jc_started": 0, "has_cards": False}


class _MergeConn:
    def __init__(self, card_floors=()):
        self.card_floors = list(card_floors)
        self.writes: list[str] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self): return None
            async def __aexit__(self, *exc): return False
        return _Tx()

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT DISTINCT p.warehouse"):
            return [{"warehouse": "A-185"}]
        if "FOR UPDATE OF l" in s:
            return [_member(1), _member(2)]
        if s.startswith("SELECT floor FROM job_card_v2"):
            return [{"floor": f} for f in self.card_floors]
        raise AssertionError(s[:80])

    async def fetchval(self, sql, *args):
        if "jcbc:table" in sql:
            return False
        if "FROM bom_process_route" in sql:      # the shared SFG, when none is typed
            return None
        raise AssertionError(sql[:80])

    async def execute(self, sql, *args):
        self.writes.append(" ".join(sql.split()))
        raise _ReachedWrite()


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


@pytest.fixture(autouse=True)
def not_a_planner(monkeypatch):
    async def check(conn, role_ids, is_admin, module, sub, subsub, action, **kw):
        return False
    monkeypatch.setattr(R, "check_permission", check)


def _merge_body(floor, pkg_floor=None):
    return R.ProcessMergeCreateRequest(
        plan_line_ids=[1, 2],
        wip_steps=[R.ProcessMergeStepIn(process="Roasting", floor=floor)],
        per_member=[R.ProcessMergeMemberIn(plan_line_id=1, pkg_floor=pkg_floor or floor),
                    R.ProcessMergeMemberIn(plan_line_id=2, pkg_floor=pkg_floor or floor)],
    )


def _merge(conn, body):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn))))
    return R.create_merged_process_run_v2(request, body, BackgroundTasks(), user=FLOOR_USER)


@pytest.mark.asyncio
async def test_lowercased_floor_now_passes_for_an_assigned_user():
    """What the wizard used to pre-fill was refused although the user has the
    floor. Floor checks ignore case, so it passes, and the run is built on the
    declared spelling."""
    conn = _MergeConn()
    with pytest.raises(_ReachedWrite):
        await _merge(conn, _merge_body("roasting area"))
    assert conn.writes == ["DELETE FROM job_card_v2 WHERE plan_line_id = ANY($1::int[])"]


@pytest.mark.asyncio
async def test_a_floor_the_user_is_not_assigned_is_still_refused():
    conn = _MergeConn()
    with pytest.raises(HTTPException) as exc:
        await _merge(conn, _merge_body("Roasting Area", pkg_floor="Sorting Area"))
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert detail["error"] == "floor_not_allowed"
    assert detail["details"] == {"requested": ["Sorting Area"], "allowed_floors": ["Roasting Area"]}
    assert "Sorting Area is not one of your floors" in detail["message"]
    assert "Your floors in A-185: Roasting Area" in detail["message"]
    assert conn.writes == []                    # refused before the members' cards go


@pytest.mark.asyncio
async def test_rebuilding_cards_already_on_that_floor_is_not_a_touch():
    """Members carded but not started: their packaging cards already sit on Sorting
    Area, so a run that keeps them there touches only what changes."""
    conn = _MergeConn(card_floors=["Roasting Area", "Sorting Area", "Roasting Area", "sorting area"])
    with pytest.raises(_ReachedWrite):
        await _merge(conn, _merge_body("Roasting Area", pkg_floor="Sorting Area"))


@pytest.mark.asyncio
async def test_a_typed_floor_that_is_no_floor_of_the_factory_is_a_400():
    conn = _MergeConn()
    with pytest.raises(HTTPException) as exc:
        await _merge(conn, _merge_body("Roasting Area", pkg_floor="Rosting Area"))
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "floor_not_in_factory"
    assert "Rosting Area is not a production floor of A-185" in exc.value.detail["message"]
    assert conn.writes == []


@pytest.mark.asyncio
async def test_stored_floor_passes_the_floor_check():
    """What it pre-fills now (the stored name): past the check, on to the merge."""
    with pytest.raises(_ReachedWrite):
        await _merge(_MergeConn(), _merge_body("Roasting Area"))
