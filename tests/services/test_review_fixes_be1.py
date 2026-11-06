"""Job cards, plan steps and merges after review, 2026-10-27.

Before:
  * Stop process left out the output saved on the open batch it cancels (a
    batch that didn't balance stays open), so the next stage was released with
    nothing while the stopped card still counted that output as made — since
    24 Oct a stop sends on everything the stage made;
  * an admin could no longer open a batch on a completed card to add one that
    was forgotten, though the screen still offers it; and Close JC went ahead
    with a batch still open, which could then be neither completed nor
    cancelled;
  * Save Output checked "output can't go below what was sent on" without
    locking the card, so a dispatch at the same moment could slip in between;
  * Merge process deleted the members' cards with no rebuild check: a
    cancelled card's recorded work went with them, and a card the floor had
    asked material for (or a BOM amendment names) gave a raw 500; the pop-up
    offered such products;
  * adding, moving or removing a plan step rewired a merged run's or a
    several-chain line's cards as one chain of their own;
  * a step's floor change checked the step's floor, not where its cards are:
    clearing the floor and then setting it moved cards off a floor nobody
    checked, and a blank or spaces floor moved them unchecked;
  * Stop, cancel and the step update took card rows before the plan line, so
    they could deadlock with a live edit (line, then its cards);
  * Plan Detail had only a line-level card count to decide which floors a step
    change touches.
Then (re-review, same day):
  * the step-change resync counted the card add_step had just spawned as a
    second chain and left it unlinked, so adding a step to any carded line
    split it in two;
  * Plan Detail could see neither where a step's cards are nor whether a line's
    steps are locked by its cards, so a save could be half applied;
  * Close's open_batch refusal named no way out the web has.
(Stop and cancel taking the stage before first: test_cancel_joins_chain.)
Services and routes are faked or scripted; no database.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import date
from types import SimpleNamespace

import pytest
from asyncpg.exceptions import ForeignKeyViolationError
from fastapi import BackgroundTasks, HTTPException

from app.modules.production import router as R
from app.modules.production.services import jc_bom_changes
from app.modules.production.services import job_card_batch_v2 as batch_svc
from app.modules.production.services import job_card_v2 as jcv2
from app.modules.production.services import plan_v2
from app.modules.production.services.refusals import ServiceRefusal

try:
    import pglast
except ImportError:  # pragma: no cover - exercised only on a box without pglast
    pglast = None

requires_pglast = pytest.mark.skipif(pglast is None, reason="pglast gives real PostgreSQL grammar validation")

CARD, NEXT = 8101, 8102


def run(coro):
    return asyncio.run(coro)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, *rest):
        self.conn.tx_exits.append(exc_type)
        return False


class _Script:
    """Answers each statement by the first fragment it contains (`rows` for
    fetchrow / fetch, `vals` for fetchval); anything else fails the test. `log`
    keeps every statement in order, `writes` the INSERT / UPDATE / DELETEs.
    `on_execute(sql, args)` may raise for an execute()."""

    def __init__(self, rows=None, vals=None, on_execute=None):
        self.rows, self.vals = rows or {}, vals or {}
        self.on_execute = on_execute
        self.log: list[tuple[str, tuple]] = []
        self.raw: list[str] = []            # as sent: a `--` comment ends at its line
        self.writes: list[str] = []
        self.tx_exits: list = []

    def transaction(self):
        return _Tx(self)

    def is_in_transaction(self):
        return True

    def _seen(self, sql, args) -> str:
        s = _norm(sql)
        self.log.append((s, args))
        self.raw.append(sql)
        if s.split()[0] in ("INSERT", "UPDATE", "DELETE"):
            self.writes.append(s)
        return s

    def _answer(self, table, kind, sql, args):
        s = self._seen(sql, args)
        for fragment, v in table.items():
            if fragment in s:
                return v(args) if callable(v) else v
        raise AssertionError(f"unexpected {kind}: {s[:120]}")

    async def fetchrow(self, sql, *args):
        return self._answer(self.rows, "fetchrow", sql, args)

    async def fetch(self, sql, *args):
        return self._answer(self.rows, "fetch", sql, args)

    async def fetchval(self, sql, *args):
        return self._answer(self.vals, "fetchval", sql, args)

    async def execute(self, sql, *args):
        s = self._seen(sql, args)
        if self.on_execute is not None:
            self.on_execute(s, args)
        return "UPDATE 1"

    def sql(self) -> list[str]:
        return [s for s, _ in self.log]


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


def _req(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn))))


def _user(*, admin=False, floors=("Roasting Area",), warehouses=("A185",)):
    return SimpleNamespace(is_admin=admin, allowed_floors=list(floors), allowed_warehouses=list(warehouses),
                           role_ids=[6], full_name="Team Leader", email=None, phone="98", user_id=44)


ADMIN = _user(admin=True, floors=(), warehouses=())


@pytest.fixture
def approves(monkeypatch):
    """Whether the user holds plans > approve (a planner) or not."""
    state = {"may": False}

    async def check(conn, role_ids, is_admin, module, sub, subsub, action, **kw):
        return state["may"]
    monkeypatch.setattr(R, "check_permission", check)
    return state


@pytest.fixture
def no_send(monkeypatch):
    async def _to_next(conn, **kw):
        raise AssertionError("nothing may be sent on")
    monkeypatch.setattr(jcv2, "dispatch_to_next", _to_next)


# ── CD-2: an admin may still add a forgotten batch to a completed card ─────

@pytest.fixture
def unlocked(monkeypatch):
    async def _not_locked(conn, job_card_id):
        return None
    monkeypatch.setattr(batch_svc, "assert_not_locked", _not_locked)


def _open_conn(status):
    return _Script(
        rows={"SELECT job_card_id, status FROM job_card_v2": {"job_card_id": CARD, "status": status},
              "INSERT INTO job_card_batch_v2": lambda a: {"batch_id": a[0], "job_card_id": a[1],
                                                         "batch_number": a[2], "status": "open"}},
        vals={"SELECT COALESCE(MAX(batch_number), 0) + 1": 4,
              "SELECT CURRENT_DATE": date(2026, 10, 27)},
    )


def test_an_admin_may_open_a_batch_on_a_completed_card(unlocked):
    """Completing it later sends on only what is left (close_batch's cap)."""
    conn = _open_conn("completed")
    out = run(batch_svc.open_batch(conn, job_card_id=CARD, is_admin=True))
    assert out["opened"] is True and out["batch"]["batch_number"] == 4
    assert conn.writes == ["INSERT INTO job_card_batch_v2 ( batch_id, job_card_id, batch_number, "
                           "batch_label, batch_date, planned_qty_kg, input_qty_kg, status, notes, "
                           "opened_at_ist ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'open', $8, $9) "
                           "RETURNING *"]


def test_anyone_else_is_refused_on_a_completed_card_and_told_who_can(unlocked):
    conn = _open_conn("completed")
    out = run(batch_svc.open_batch(conn, job_card_id=CARD))
    assert out["error"] == "job_card_finished"
    assert out["message"] == (f"Job card {CARD} is completed, so a new batch can't be opened on it. "
                              "Plan any further production on a new job card, or ask an admin to "
                              "add a batch that was missed.")
    assert conn.writes == []


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_not_even_an_admin_opens_one_on_a_closed_or_cancelled_card(unlocked, status):
    conn = _open_conn(status)
    out = run(batch_svc.open_batch(conn, job_card_id=CARD, is_admin=True))
    assert out["error"] == "job_card_finished" and out["status"] == status
    assert conn.writes == []


def test_the_open_batch_route_says_whether_the_user_is_an_admin(monkeypatch):
    seen = []

    async def _open(conn, **kw):
        seen.append(kw["is_admin"])
        return {"opened": True, "batch": {}}
    monkeypatch.setattr(batch_svc, "open_batch", _open)
    for user in (ADMIN, _user()):
        run(R.batch_open_v2(_req(_Script()), CARD, None, user=user))
    assert seen == [True, False]


# ── CD-4: Close JC waits for an open batch ─────────────────────────────────

def _close_conn(open_batch):
    card = {"job_card_id": CARD, "status": "completed", "plan_id": 9, "next_job_card_id": NEXT,
            "output_kind": "WIP", "dispatched_to_next_kg": 20.0, "process_group_id": None}
    return _Script(
        rows={"SELECT job_card_id, status, plan_id, next_job_card_id": card,
              "SELECT batch_id, batch_number, COALESCE(NULLIF(btrim(batch_label)": open_batch,
              "SELECT role FROM job_card_sign_off_v2": [{"role": "production_head"}]},
        vals={"SELECT COUNT(*) FROM job_card_shift_log_v2": 0},
    )


def test_a_card_with_an_open_batch_is_not_closed(no_send):
    """It used to close and send the open batch's 30 kg on; the batch then could
    be neither completed (the card is closed) nor cancelled (it has output)."""
    conn = _close_conn({"batch_id": 55, "batch_number": 3, "name": "55"})
    out = run(jcv2.close_job_card(conn, job_card_id=CARD, closed_by="Ravi K"))
    assert out["error"] == "open_batch" and out["batch_number"] == 3
    # A batch with nothing recorded on it is cancelled on the Accounting tab, which
    # names an unlabelled batch by its code
    # (POST .../batches/{id}/cancel refuses one with output, boxes or shift time).
    assert out["message"] == ("Batch 55 is still open. Complete it, or use Cancel batch on the job "
                              "card's Accounting tab if nothing has been recorded on it yet, "
                              "before closing this job card.")
    assert conn.writes == []
    # Asked under the card's lock, which the close takes first.
    assert conn.sql()[0].endswith("FOR UPDATE")
    assert conn.sql()[1] == ("SELECT batch_id, batch_number, "
                             "COALESCE(NULLIF(btrim(batch_label), ''), batch_id::text) AS name "
                             "FROM job_card_batch_v2 WHERE job_card_id = $1 "
                             "AND status = 'open' ORDER BY batch_number LIMIT 1")


def test_the_close_route_refuses_an_open_batch_with_a_409_inside_its_transaction(monkeypatch):
    async def refuse(conn, **kw):
        return {"error": "open_batch", "batch_id": 55, "batch_number": 3,
                "message": "Batch 55 is still open. Complete it, or use Cancel batch on the job "
                           "card's Accounting tab if nothing has been recorded on it yet, "
                           "before closing this job card."}

    async def in_scope(conn, **kw):
        return None
    monkeypatch.setattr(jcv2, "close_job_card", refuse)
    monkeypatch.setattr(R, "_assert_jc_writable_by_user", in_scope)
    conn = _Script()
    with pytest.raises(HTTPException) as e:
        run(R.close_job_card_v2(_req(conn), CARD, BackgroundTasks(), None, user=_user()))
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "open_batch"
    assert conn.tx_exits == [HTTPException]              # raised inside: rolled back


# ── CC-5: the output check runs under the card's lock ──────────────────────

def _output_check_conn(dispatched):
    return _Script(vals={"SELECT dispatched_to_next_kg FROM job_card_v2": dispatched,
                         "SELECT COALESCE(SUM(output_qty_kg), 0) FROM job_card_output_v2": 10.0})


def test_save_output_locks_the_card_before_it_checks_what_was_sent_on():
    """The card's box lock, then its row FOR UPDATE — close_batch's order (box
    lock, batch, card) — then the check. A dispatch locks the card row too, so
    it waits for this save and sums the new output."""
    conn = _output_check_conn(50.0)
    body = R.RecordOutputV2Request(output_qty_kg=30.0, batch_id=55)
    with pytest.raises(HTTPException) as e:
        run(R._refuse_output_below_dispatched(conn, CARD, 55, body))
    assert e.value.status_code == 409 and e.value.detail["error"] == "output_below_dispatched"
    assert conn.log[0] == ("SELECT pg_advisory_xact_lock($1)", (CARD,))
    assert conn.log[1] == ("SELECT dispatched_to_next_kg FROM job_card_v2 WHERE job_card_id = $1 "
                           "FOR UPDATE", (CARD,))
    assert conn.sql()[2].startswith("SELECT COALESCE(SUM(output_qty_kg), 0)")
    assert conn.writes == []


def test_the_card_is_locked_even_when_nothing_was_sent_on_yet():
    """Unlocked, 'nothing sent yet' was read, a dispatch of the old output
    committed, and the save then took the output below it."""
    conn = _output_check_conn(0.0)
    run(R._refuse_output_below_dispatched(conn, CARD, 55, R.RecordOutputV2Request(output_qty_kg=5.0)))
    assert conn.sql() == ["SELECT pg_advisory_xact_lock($1)",
                          "SELECT dispatched_to_next_kg FROM job_card_v2 WHERE job_card_id = $1 FOR UPDATE"]


def test_a_save_with_no_fg_kg_locks_nothing():
    conn = _output_check_conn(50.0)
    run(R._refuse_output_below_dispatched(conn, CARD, 55, R.RecordOutputV2Request(notes="x")))
    assert conn.log == []


def test_record_output_reads_the_card_for_update(monkeypatch):
    async def _not_locked(conn, job_card_id):
        return None
    monkeypatch.setattr(jcv2, "assert_not_locked", _not_locked)
    conn = _Script(rows={"SELECT job_card_id, output_kind, dispatched_to_next_kg FROM job_card_v2":
                         {"job_card_id": CARD, "output_kind": "WIP", "dispatched_to_next_kg": 50.0}},
                   vals={"SELECT COALESCE(SUM(output_qty_kg), 0)": 0.0})
    out = run(jcv2.record_output(conn, job_card_id=CARD, rm_consumed_kg=60.0, output_qty_kg=40.0,
                                 batch_id=55))
    assert out["error"] == "output_below_dispatched"
    assert conn.sql()[0] == ("SELECT job_card_id, output_kind, dispatched_to_next_kg FROM job_card_v2 "
                             "WHERE job_card_id=$1 AND deleted_at IS NULL FOR UPDATE")


def test_the_outputs_route_takes_the_plan_line_before_the_card():
    """line -> card's box lock -> card: the order the live edit (line, cards) and
    close_batch (box lock, batch, card) take, so none of them deadlock."""
    src = inspect.getsource(R.record_output_v2)
    assert src.index("lock_line_for_card(conn, job_card_id)") < src.index("_refuse_output_below_dispatched(")


# ── CC-4: stop, cancel and the step update take the plan line first ────────

def _line_lock_conn():
    return _Script(vals={"jcbc:lock_card_share": 70})


@pytest.mark.parametrize("route", ["stop", "cancel"])
def test_stop_and_cancel_queue_behind_a_live_edit_of_the_line(monkeypatch, route):
    """A live edit holds the line, then locks the cards in step order; a stop
    held its card and then wanted the one before — a deadlock, one side a 500.
    Now the stop / cancel takes the line (KEY SHARE) before any card."""
    conn = _line_lock_conn()
    seen = []

    async def _service(c, **kw):
        seen.append(list(c.sql()))
        return {"stopped": True, "cancelled": True, "unlocked_job_card_id": None}
    if route == "stop":
        monkeypatch.setattr(jcv2, "stop_job_card", _service)
        run(R.stop_jc_v2(_req(conn), CARD, R.StopJobCardV2Request(reason="Machine down"),
                         BackgroundTasks(), user=_user()))
    else:
        monkeypatch.setattr(jcv2, "cancel_job_card", _service)
        run(R.cancel_jc_v2(_req(conn), CARD, R.CancelJobCardV2Request(reason="Wrong process"),
                           BackgroundTasks(), user=ADMIN))
    assert seen == [[_norm(jc_bom_changes._LOCK_CARD_SHARE_SQL)]]   # the line, before the service
    assert "FOR KEY SHARE OF l" in seen[0][0]
    assert conn.log[0][1] == (CARD,)


# ── FA-1 / FA-2: a step's floor change checks where its cards are ──────────

class _StepConn(_Script):
    def __init__(self, row):
        super().__init__(rows={"FROM production_plan_step_v2 s": row})


@pytest.fixture
def updates(monkeypatch):
    wrote = []

    async def update(conn, step_id, fields):
        wrote.append(dict(fields))
        return {"updated": True}
    monkeypatch.setattr(plan_v2, "update_step", update)
    return wrote


def _put_floor(row, floor, user=None):
    conn = _StepConn(row)
    run(R.update_step_v2(_req(conn), 61, R.StepV2Patch(floor=floor), user=user or _user()))
    return conn


SORTING_CARDS = {"floor": "Sorting Area", "warehouse": "A-185", "card_floors": ["Sorting Area"]}


def test_clearing_the_floor_then_setting_it_still_checks_the_floor_the_cards_leave(approves, updates):
    """Save 1 clears the step's floor (the cards stay on Sorting Area); save 2
    sets Roasting Area. The step's floor was empty, so nothing was checked, and
    the cards moved off Sorting Area."""
    _put_floor(SORTING_CARDS, None)
    assert updates == [{"floor": None}]                 # step cleared, nothing checked
    cleared = {**SORTING_CARDS, "floor": None}          # the cards are still on Sorting Area
    with pytest.raises(HTTPException) as exc:
        _put_floor(cleared, "Roasting Area")
    assert exc.value.status_code == 403
    assert exc.value.detail["details"]["requested"] == ["Sorting Area"]
    assert updates == [{"floor": None}]                 # the second save wrote nothing


def test_a_card_moved_off_its_steps_floor_is_checked_where_it_is(approves, updates):
    """The step says Roasting Area, its card is on Sorting Area (moved by PATCH):
    re-sending Roasting Area moves the card, so Sorting Area is checked."""
    with pytest.raises(HTTPException) as exc:
        _put_floor({**SORTING_CARDS, "floor": "Roasting Area"}, "Roasting Area")
    assert exc.value.detail["details"]["requested"] == ["Sorting Area"]
    assert updates == []


@pytest.mark.parametrize("blank", ["   ", ""])
def test_a_blank_floor_clears_the_step_and_leaves_its_cards(approves, updates, blank):
    """It was written onto the cards: they left Sorting Area for a blank floor,
    unchecked (spaces were refused before this change, '' never was)."""
    _put_floor(SORTING_CARDS, blank)
    assert updates == [{"floor": None}]                 # NULL: _sync_jc_attrs_from_step moves no card


def test_a_planner_may_move_the_cards_to_any_floor_of_the_factory(approves, updates):
    approves["may"] = True
    _put_floor({**SORTING_CARDS, "floor": None}, "roasting area")
    assert updates == [{"floor": "Roasting Area"}]      # the declared spelling


def test_the_cards_spelling_is_kept_when_they_stay_put(approves, updates):
    """The cards are on 'roasting area' as stored: nothing moves, the step takes
    their spelling, and only that floor (the user's) is checked."""
    _put_floor({"floor": None, "warehouse": "A-185", "card_floors": ["roasting area"]}, "Roasting Area")
    assert updates == [{"floor": "roasting area"}]


def test_the_new_floor_is_checked_when_the_cards_move_onto_it(approves, updates):
    with pytest.raises(HTTPException) as exc:
        _put_floor({"floor": "Roasting Area", "warehouse": "A-185", "card_floors": ["Roasting Area"]},
                   "Sorting Area")
    assert exc.value.detail["details"]["requested"] == ["Sorting Area"]


def test_an_unchanged_floor_with_no_card_to_move_checks_nothing(approves, updates):
    _put_floor({"floor": "Sorting Area", "warehouse": "A-185", "card_floors": []}, "sorting area")
    assert updates == [{"floor": "Sorting Area"}]


def test_the_step_update_takes_the_plan_line_first(approves, updates):
    """It writes the step, then its cards; a live edit holds the cards, then
    writes the steps. FOR KEY SHARE on the line queues it behind the edit."""
    conn = _put_floor(SORTING_CARDS, None)
    assert conn.log[0] == (_norm(R._STEP_FACTORY_SQL), (61,))
    assert conn.log[0][0].endswith("FOR KEY SHARE OF l")


# ── SFG-1 / CC-2 / CD-6: Merge process can't delete cards it must keep ─────

NOTHING_BLOCKS = {"cancelled": 0, "requested_on": None, "amended_on": None}


def _member(plan_line_id, *, has_cards=False):
    return {"plan_line_id": plan_line_id, "plan_id": 900, "bom_id": 70 + plan_line_id,
            "fg_sku_name": f"FG {plan_line_id}", "customer_name": None, "planned_qty_kg": 100,
            "planned_qty_units": None, "factory": "a-185", "entity": "cfpl", "entity_raw": "cfpl",
            "warehouse_raw": "A-185", "plan_status": "approved", "floor1": "roasting area",
            "rm_fp": "sunflower seeds", "jc_started": 0, "has_cards": has_cards}


def _merge_conn(members, blockers=None, *, delete_fails=False):
    blockers = blockers or {}
    asked = []

    def _blockers(args):
        asked.append(args[0])
        return blockers.get(args[0], NOTHING_BLOCKS)

    def _execute(s, args):
        if delete_fails and s.startswith("DELETE FROM job_card_v2"):
            raise ForeignKeyViolationError("violates foreign key constraint floor_requisition_job_card_id_fkey")
        if s.startswith(("DELETE", "INSERT", "UPDATE")) and not delete_fails:
            raise AssertionError("refused before any write: " + s[:80])

    conn = _Script(
        rows={"SELECT DISTINCT p.warehouse": [{"warehouse": "A-185"}],
              "FOR UPDATE OF l": members,
              "SELECT floor FROM job_card_v2": [],
              "AS requested_on": _blockers},
        vals={"to_regclass('public.floor_requisition')": True, "jcbc:table": False},
        on_execute=_execute)
    conn.asked = asked
    return conn


def _merge_args():
    return dict(plan_line_ids=[1, 2],
                wip_steps=[{"process": "Roasting", "floor": "Roasting Area", "sfg_output": "Roasted Seeds"}],
                per_member=[{"plan_line_id": 1, "pkg_floor": "FG store"},
                            {"plan_line_id": 2, "pkg_floor": "FG store"}])


def test_a_product_with_cancelled_cards_is_refused_before_anything_is_deleted():
    """The DELETE took its cancelled cards and their batches, outputs and cancel
    snapshot with it, silently."""
    conn = _merge_conn([_member(1), _member(2, has_cards=True)], {2: {**NOTHING_BLOCKS, "cancelled": 2}})
    out = run(jcv2.create_merged_process_run(conn, **_merge_args()))
    assert out["error"] == "rebuild_blocked" and out["plan_line_id"] == 2
    assert out["message"] == ("FG 2: This article has cancelled job cards whose records are kept, so "
                              "its chain can't be rebuilt. Leave it out of the merge.")
    assert conn.writes == []
    assert conn.asked == [2]                            # a product with no cards isn't asked


def test_a_product_the_floor_asked_material_for_is_refused_by_name():
    conn = _merge_conn([_member(1, has_cards=True), _member(2)],
                       {1: {**NOTHING_BLOCKS, "requested_on": "Roasting (job card PLAN-9-L1-S1)"}})
    out = run(jcv2.create_merged_process_run(conn, **_merge_args()))
    assert out["error"] == "rebuild_blocked"
    assert out["message"] == ("FG 1: The floor has already asked for material for Roasting (job card "
                              "PLAN-9-L1-S1), so this chain can't be rebuilt. Leave it out of the merge.")
    assert conn.writes == []


def test_a_requisition_raised_after_the_check_is_a_409_not_a_500():
    """The DELETE's foreign key error used to reach the user as a raw 500."""
    conn = _merge_conn([_member(1, has_cards=True), _member(2, has_cards=True)], delete_fails=True)
    with pytest.raises(HTTPException) as e:
        run(R.create_merged_process_run_v2(_req(conn), R.ProcessMergeCreateRequest(
            plan_line_ids=[1, 2],
            wip_steps=[R.ProcessMergeStepIn(process="Roasting", floor="Roasting Area",
                                            sfg_output="Roasted Seeds")],
            per_member=[R.ProcessMergeMemberIn(plan_line_id=1, pkg_floor="FG store"),
                        R.ProcessMergeMemberIn(plan_line_id=2, pkg_floor="FG store")]),
            BackgroundTasks(), user=ADMIN))
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "rebuild_blocked"
    assert "can't be merged" in e.value.detail["message"]
    assert conn.tx_exits == [ServiceRefusal]            # raised inside the transaction: rolled back
    # Unfiltered on purpose: a cancelled card left behind would hold its step.
    assert conn.writes == ["DELETE FROM job_card_v2 WHERE plan_line_id = ANY($1::int[])"]


def test_the_service_raises_the_fk_refusal_rather_than_returning_half_a_merge():
    conn = _merge_conn([_member(1, has_cards=True), _member(2, has_cards=True)], delete_fails=True)
    with pytest.raises(ServiceRefusal) as e:
        run(jcv2.create_merged_process_run(conn, **_merge_args()))
    assert e.value.result["error"] == "rebuild_blocked"


def _validate_line(plan_line_id, *, has_cards=False, jc_count=0):
    return {"plan_line_id": plan_line_id, "plan_id": 900, "fg_sku_name": f"FG {plan_line_id}",
            "bom_id": 70 + plan_line_id, "planned_qty_kg": 100, "planned_qty_units": None,
            "plan_status": "approved", "factory": "a-185", "entity": "cfpl", "floor1": "roasting area",
            "rm_fingerprint": "sunflower seeds", "jc_count": jc_count, "jc_started": 0,
            "has_cards": has_cards}


def test_the_merge_pop_up_never_offers_a_product_it_would_refuse():
    lines = [_validate_line(1), _validate_line(2),
             _validate_line(3, has_cards=True),                  # only cancelled cards: jc_count 0
             _validate_line(4, has_cards=True, jc_count=2)]      # carded, nothing blocks: offered
    blockers = {3: {**NOTHING_BLOCKS, "cancelled": 1}}
    conn = _Script(
        rows={"FROM production_plan_line_v2 l": lines,
              "SELECT plan_line_id, step_order": [
                  {"plan_line_id": i, "step_order": 1, "process_name": "Roasting", "stage": "roasting",
                   "floor": "Roasting Area"} for i in (1, 2, 3, 4)],
              "AS requested_on": lambda a: blockers.get(a[0], NOTHING_BLOCKS),
              "output_kind = 'FG'": [],
              "FROM bom_process_route": [],
              "FROM sfg_canonical_map": None},
        vals={"to_regclass('public.floor_requisition')": True})
    out = run(plan_v2.validate_process_merge(conn, [900]))
    assert [m["plan_line_id"] for m in out["groups"][0]["members"]] == [1, 2, 4]
    assert [(i["plan_line_id"], i["reason"]) for i in out["ineligible"]] == [
        (3, "This article has cancelled job cards whose records are kept, so its chain can't be rebuilt.")]
    asked = [a[0] for s, a in conn.log if "AS requested_on" in s]
    assert asked == [3, 4]                              # only lines with cards


# ── SFG-3: plan steps of a merged run or a several-chain line stay put ─────

def _jc(jid, step_id, *, prev=None, group=None, status="locked"):
    return {"job_card_id": jid, "plan_step_id": step_id, "step_number": 1, "status": status,
            "prev_job_card_id": prev, "next_job_card_id": None, "output_code": None,
            "process_group_id": group}


MERGED_MEMBER = [_jc(601, 41, prev=500, group=9)]                    # packaging fed by the run
TWO_CHAINS = [_jc(611, 41), _jc(612, 42, prev=611), _jc(621, 41), _jc(622, 42, prev=621)]


def _steps_conn(cards):
    return _Script(
        rows={"FROM job_card_v2": cards,
              "SELECT step_id FROM production_plan_step_v2": [{"step_id": 41}, {"step_id": 42}],
              "SELECT plan_line_id, step_order FROM production_plan_step_v2":
                  {"plan_line_id": 73, "step_order": 2}},
        vals={"MAX(step_order)": 3})


def test_adding_a_step_to_a_merged_runs_line_is_refused_before_it_is_written():
    """It spawned a card and rewired the line's packaging as a chain of its own:
    cut off from the shared process card, with no RM indent — never made."""
    conn = _steps_conn(MERGED_MEMBER)
    out = run(plan_v2.add_step(conn, 73, {"process_name": "Metal Detection", "floor": "FG store"}))
    assert out["error"] == "steps_locked_by_cards" and out["reason"] == "merged_run"
    assert out["message"].startswith("This article's job cards are part of a merged process run")
    assert conn.writes == []


def test_reordering_a_several_chain_line_is_refused_before_it_is_written():
    """The resync walked every chain's cards as one chain and joined them."""
    conn = _steps_conn(TWO_CHAINS)
    out = run(plan_v2.reorder_steps(conn, 73, [42, 41]))
    assert out["error"] == "steps_locked_by_cards"
    assert (out["reason"], out["chains"]) == ("multiple_chains", 2)
    assert out["message"].startswith("This article was carded in 2 separate chains")
    assert conn.writes == []


@pytest.mark.parametrize("cards", [MERGED_MEMBER, TWO_CHAINS])
def test_deleting_a_step_of_such_a_line_is_refused_before_it_is_written(cards):
    conn = _steps_conn(cards)
    out = run(plan_v2.delete_step(conn, 42))
    assert out["error"] == "steps_locked_by_cards"
    assert conn.writes == []


def test_the_resync_never_rewires_such_a_line():
    conn = _steps_conn(MERGED_MEMBER)
    out = run(plan_v2._resync_jcs_after_step_change(conn, 73, reason="reorder"))
    assert out["applied"] is False and out["skipped_reason"] == "merged_run"
    assert conn.writes == []


class _AddStepConn(_Script):
    """plan_v2.add_step on a carded line, with state: the line's plan steps and
    live cards, the spawned card, and what the resync writes onto them."""

    def __init__(self, cards):
        super().__init__()
        self.steps = {c["plan_step_id"]: {"step_id": c["plan_step_id"], "step_order": i + 1,
                                          "process_name": f"Step {i + 1}", "stage": f"step_{i + 1}",
                                          "floor": "Roasting Area"}
                      for i, c in enumerate(cards)}
        self.cards = {c["job_card_id"]: dict(c) for c in cards}

    def _live(self):
        return sorted((dict(c) for c in self.cards.values()), key=lambda c: c["step_number"])

    async def fetch(self, sql, *args):
        s = self._seen(sql, args)
        if "FROM job_card_v2 WHERE plan_line_id = $1 AND deleted_at IS NULL" in s:
            return self._live()
        if "FROM production_plan_step_v2 WHERE plan_line_id = $1 ORDER BY step_order" in s:
            return sorted(self.steps.values(), key=lambda r: r["step_order"])
        raise AssertionError(f"unexpected fetch: {s[:120]}")

    async def fetchrow(self, sql, *args):
        s = self._seen(sql, args)
        if s.startswith("INSERT INTO production_plan_step_v2"):
            _sid, _line, order, process, stage, floor, *_ = args
            self.steps[43] = {"step_id": 43, "step_order": order, "process_name": process,
                              "stage": stage, "floor": floor}
            return {"plan_line_id": 73, **self.steps[43]}
        if "FROM production_plan_line_v2 l JOIN production_plan_v2 p" in s:
            return {"plan_line_id": 73, "plan_id": 900, "fg_sku_name": "FG", "customer_name": None,
                    "bom_id": 71, "planned_qty_kg": 100, "planned_qty_units": None,
                    "entity": "cfpl", "warehouse": "A-185"}
        if "FROM production_plan_step_v2 WHERE step_id = $1" in s:
            return self.steps[args[0]]
        raise AssertionError(f"unexpected fetchrow: {s[:120]}")

    async def fetchval(self, sql, *args):
        s = self._seen(sql, args)
        if "COALESCE(MAX(step_order), 0) + 1" in s:
            return max(r["step_order"] for r in self.steps.values()) + 1
        if s.startswith("SELECT COUNT(*) FROM job_card_v2"):
            return len(self.cards)
        if s.startswith("INSERT INTO job_card_v2"):
            # No prev_job_card_id: linking the new card is the resync's job.
            assert "prev_job_card_id" not in s
            self.cards[703] = {"job_card_id": 703, "plan_step_id": args[4], "step_number": args[6],
                               "status": "locked", "prev_job_card_id": None, "next_job_card_id": None,
                               "output_code": None, "process_group_id": None,
                               "input_kind": args[15], "output_kind": args[16]}
            return 703
        if s.startswith("SELECT bom_id FROM production_plan_line_v2"):
            return 71
        if "FROM bom_process_route" in s:
            return None                                 # not a routed 2-stage article
        raise AssertionError(f"unexpected fetchval: {s[:120]}")

    async def execute(self, sql, *args):
        s = self._seen(sql, args)
        if s.startswith("UPDATE job_card_v2 SET step_number"):
            jid, step, prev, in_kind, out_kind = args[:5]
            self.cards[jid].update(step_number=step, prev_job_card_id=prev,
                                   input_kind=in_kind, output_kind=out_kind)
        elif s.startswith("UPDATE job_card_v2 SET next_job_card_id"):
            self.cards[args[0]]["next_job_card_id"] = args[1]
        else:
            raise AssertionError(f"unexpected execute: {s[:120]}")
        return "UPDATE 1"


def _chain_card(jid, step_id, step_number, *, prev, nxt, status, out_kind):
    return {**_jc(jid, step_id, prev=prev, status=status), "step_number": step_number,
            "next_job_card_id": nxt, "input_kind": "RM" if prev is None else "SFG",
            "output_kind": out_kind}


ROAST_THEN_PACK = [_chain_card(701, 41, 1, prev=None, nxt=702, status="unlocked", out_kind="WIP"),
                   _chain_card(702, 42, 2, prev=701, nxt=None, status="locked", out_kind="FG")]
PACK_ONLY = [_chain_card(701, 41, 1, prev=None, nxt=None, status="unlocked", out_kind="FG")]


@pytest.mark.parametrize("cards", [ROAST_THEN_PACK, PACK_ONLY], ids=["two_cards", "one_fg_card"])
def test_a_step_added_to_a_carded_line_links_its_new_card_after_the_tail(cards):
    """The new card is a second head until the resync links it. The resync
    counted it as a second chain and skipped: the card stayed locked with
    nothing upstream, the line had two FG cards, and every later step change
    and Edit Job Card was refused as 'carded in 2 separate chains'."""
    conn = _AddStepConn(cards)
    out = run(plan_v2.add_step(conn, 73, {"process_name": "Metal Detection", "floor": "FG store"}))
    assert out["spawned_job_card_id"] == 703
    n = len(cards) + 1
    assert out["jc_sync"] == {"applied": True, "reason": "add_step", "updated": n, "line_jc_count": n}
    chain = [c["job_card_id"] for c in cards] + [703]
    got = {j: conn.cards[j] for j in chain}
    assert [got[j]["prev_job_card_id"] for j in chain] == [None] + chain[:-1]
    assert [got[j]["next_job_card_id"] for j in chain] == chain[1:] + [None]
    assert [got[j]["step_number"] for j in chain] == list(range(1, n + 1))
    assert [(got[j]["input_kind"], got[j]["output_kind"]) for j in chain] == (
        [("RM", "WIP")] + [("SFG", "WIP")] * (n - 2) + [("SFG", "FG")])
    # The several-chain refusal still runs on the cards before the step is written.
    assert plan_v2._steps_locked_refusal(cards) is None


def test_one_chain_is_not_refused():
    assert plan_v2._steps_locked_refusal([_jc(611, 41), _jc(612, 42, prev=611)]) is None
    assert plan_v2._steps_locked_refusal([]) is None


@pytest.mark.parametrize("route", ["add", "reorder", "delete"])
def test_the_step_routes_answer_409(monkeypatch, route):
    refusal = {"error": "steps_locked_by_cards", "reason": "merged_run", "message": "Leave the steps."}

    async def _refuse(conn, *a, **kw):
        return refusal
    conn = _Script(vals={"FROM production_plan_line_v2 l": "A-185"})
    with pytest.raises(HTTPException) as e:
        if route == "add":
            monkeypatch.setattr(plan_v2, "add_step", _refuse)
            run(R.add_step_v2(_req(conn), 73, R.StepV2Add(process_name="Metal Detection"),
                              BackgroundTasks(), user=ADMIN))
        elif route == "reorder":
            monkeypatch.setattr(plan_v2, "reorder_steps", _refuse)
            run(R.reorder_steps_v2(_req(conn), 73, R.StepV2Reorder(step_ids=[42, 41]), user=ADMIN))
        else:
            monkeypatch.setattr(plan_v2, "delete_step", _refuse)
            run(R.delete_step_v2(_req(conn), 42, user=ADMIN))
    assert e.value.status_code == 409 and e.value.detail == refusal


# ── Plan Detail: which steps' cards move with them ─────────────────────────

def _plan_line(plan_line_id, *, heads=1, merged=False):
    return {"plan_line_id": plan_line_id, "plan_id": 900, "job_card_count": heads, "in_merged_run": merged}


# The floors PUT /plans-v2/steps/{id} checks: where the step's waiting cards are.
CARD_FLOORS_SQL = ("ARRAY(SELECT DISTINCT j.floor FROM job_card_v2 j WHERE j.plan_step_id = s.step_id "
                   "AND j.deleted_at IS NULL AND j.status IN ('locked', 'unlocked')) AS card_floors")


def test_each_plan_step_says_whether_its_cards_move_with_it_and_where_they_are():
    """Plan Detail checked the step's own floor; the step PUT checks the floors its
    cards are on (a cleared step floor leaves them where they were)."""
    conn = _Script(rows={
        "FROM production_plan_v2 WHERE plan_id": {"plan_id": 900, "status": "draft"},
        "FROM production_plan_line_v2 l": [_plan_line(73)],
        "FROM production_plan_step_v2 s": [
            {"step_id": 41, "plan_line_id": 73, "step_order": 1, "floor": "Sorting Area", "carded": False,
             "card_floors": []},
            {"step_id": 42, "plan_line_id": 73, "step_order": 2, "floor": None, "carded": True,
             "card_floors": ["Sorting Area"]}],
    })
    out = run(plan_v2.get_plan(conn, 900))
    assert [(s["step_id"], s["carded"], s["card_floors"]) for s in out["lines"][0]["steps"]] == [
        (41, False, []), (42, True, ["Sorting Area"])]
    steps_sql = next(s for s in conn.sql() if "FROM production_plan_step_v2 s" in s)
    # The cards update_step moves (_sync_jc_attrs_from_step): live, locked / unlocked.
    assert ("EXISTS (SELECT 1 FROM job_card_v2 j WHERE j.plan_step_id = s.step_id AND j.deleted_at "
            "IS NULL AND j.status IN ('locked', 'unlocked')) AS carded") in steps_sql
    assert CARD_FLOORS_SQL in steps_sql
    assert CARD_FLOORS_SQL.replace(" AS card_floors", "") in _norm(R._STEP_FACTORY_SQL)


def test_each_plan_line_says_whether_its_steps_are_locked_by_its_cards():
    """Plan Detail sent the step PUTs (committed) before the reorder / add that
    then came back 409 steps_locked_by_cards: half a save under 'Save failed'."""
    conn = _Script(rows={
        "FROM production_plan_v2 WHERE plan_id": {"plan_id": 900, "status": "draft"},
        "FROM production_plan_line_v2 l": [_plan_line(71, heads=0), _plan_line(72), _plan_line(73, heads=2),
                                           _plan_line(74, heads=0, merged=True),
                                           _plan_line(75, heads=2, merged=True)],
        "FROM production_plan_step_v2 s": [],
    })
    out = run(plan_v2.get_plan(conn, 900))
    locked = {ln["plan_line_id"]: ln["steps_locked"] for ln in out["lines"]}
    merged = plan_v2._steps_locked_refusal(MERGED_MEMBER)
    chains = plan_v2._steps_locked_refusal(TWO_CHAINS)
    # The step routes' own sentence, reason first (merged run before several chains).
    assert locked == {71: None, 72: None,
                      73: {"reason": "multiple_chains", "message": chains["message"]},
                      74: {"reason": "merged_run", "message": merged["message"]},
                      75: {"reason": "merged_run", "message": merged["message"]}}
    assert all("in_merged_run" not in ln for ln in out["lines"])
    # One query for the whole plan: the line SELECT says it, per line.
    lines_sql = [s for s in conn.sql() if "FROM production_plan_line_v2 l" in s]
    assert len(lines_sql) == 1
    assert ("EXISTS (SELECT 1 FROM job_card_v2 j WHERE j.plan_line_id = l.plan_line_id AND j.deleted_at "
            "IS NULL AND j.process_group_id IS NOT NULL) AS in_merged_run") in lines_sql[0]


# ── the new and changed SQL is valid PostgreSQL ────────────────────────────

@requires_pglast
def test_the_new_sql_parses(approves, updates, unlocked, monkeypatch):
    sqls = [R._STEP_FACTORY_SQL, jcv2._CARD_BOX_LOCK_SQL, jcv2._DISPATCHED_FOR_UPDATE_SQL,
            jcv2._PREV_CARD_LOCK_SQL, jcv2._CARD_FOR_CANCEL_SQL + " FOR UPDATE"]
    conn = _close_conn({"batch_id": 55, "batch_number": 3, "name": "55"})
    run(jcv2.close_job_card(conn, job_card_id=CARD))
    sqls += conn.raw
    conn = _merge_conn([_member(1), _member(2, has_cards=True)], {2: {**NOTHING_BLOCKS, "cancelled": 1}})
    run(jcv2.create_merged_process_run(conn, **_merge_args()))
    sqls += conn.raw
    conn = _steps_conn(TWO_CHAINS)
    run(plan_v2.reorder_steps(conn, 73, [42, 41]))
    sqls += conn.raw
    conn = _Script(rows={"FROM production_plan_v2 WHERE plan_id": {"plan_id": 900},
                         "FROM production_plan_line_v2 l": [_plan_line(73)],
                         "FROM production_plan_step_v2 s": []})
    run(plan_v2.get_plan(conn, 900))
    sqls += conn.raw
    conn = _Script(rows={"FROM production_plan_line_v2 l": [_validate_line(3, has_cards=True)],
                         "SELECT plan_line_id, step_order": [],
                         "AS requested_on": {**NOTHING_BLOCKS, "cancelled": 1}},
                   vals={"to_regclass('public.floor_requisition')": True})
    run(plan_v2.validate_process_merge(conn, [900]))
    sqls += conn.raw
    assert len(sqls) > 15
    sqls.append("DELETE FROM job_card_v2 WHERE plan_line_id = ANY($1::int[])")
    for sql in sqls:
        pglast.parse_sql(sql)
