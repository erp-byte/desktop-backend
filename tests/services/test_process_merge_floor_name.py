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
        "factory": "a-185", "entity": "cfpl",
        "floor1": floor_stored.strip().lower(),
        "rm_fingerprint": "sunflower seeds",
        "jc_count": 0, "jc_started": 0,
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
        return self.lines


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


# The merge endpoint's floor check, for a non-admin assigned to one floor.
FLOOR_USER = SimpleNamespace(is_admin=False, allowed_floors=["Roasting Area"], allowed_warehouses=[],
                             full_name="Floor Manager", email="", phone="", user_id=31)


class _ReachedService(Exception):
    """Raised by the fake pool: the request got past the floor check."""


class _Pool:
    def acquire(self):
        raise _ReachedService()


def _merge_body(floor):
    return R.ProcessMergeCreateRequest(
        plan_line_ids=[1, 2],
        wip_steps=[R.ProcessMergeStepIn(process="Roasting", floor=floor)],
        per_member=[R.ProcessMergeMemberIn(plan_line_id=1, pkg_floor=floor),
                    R.ProcessMergeMemberIn(plan_line_id=2, pkg_floor=floor)],
    )


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool())))


@pytest.mark.asyncio
async def test_lowercased_floor_is_refused_for_an_assigned_user():
    """What the wizard used to pre-fill: refused although the user has the floor."""
    with pytest.raises(HTTPException) as exc:
        await R.create_merged_process_run_v2(_request(), _merge_body("roasting area"),
                                             BackgroundTasks(), user=FLOOR_USER)
    assert exc.value.status_code == 403
    assert "roasting area" in exc.value.detail


@pytest.mark.asyncio
async def test_stored_floor_passes_the_floor_check():
    """What it pre-fills now (the stored name): past the check, on to the merge."""
    with pytest.raises(_ReachedService):
        await R.create_merged_process_run_v2(_request(), _merge_body("Roasting Area"),
                                             BackgroundTasks(), user=FLOOR_USER)
