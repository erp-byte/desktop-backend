"""Close and dispatch follow one limit: what a card made minus what it sent on.

Before (audit 2026-10-27): the job card screen offered CLOSE JC only when 0.05 kg
or less was left, while Dispatch to next had no cap at all until the first
output was saved, so a dispatch could mint WIP stock that was never made. The
server used 0.000001 kg in the dispatch cap and 0.001 kg in the send-on.

Now every dispatch (Dispatch to next, Distribute, the batch-close auto-dispatch,
the v1 day-end dispatch) is capped at recorded output, one epsilon (half a gram,
fg_from_boxes.SAME_KG) decides "nothing left", only a completed card can be
closed, a finished card takes no new batch, and output can't be saved below what
was already sent on.

No database: each fake connection answers only the statements its service sends.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import day_end
from app.modules.production.services import fg_from_boxes
from app.modules.production.services import job_card_batch_v2 as batch_svc
from app.modules.production.services import job_card_v2 as jcv2

CARD, NEXT = 7101, 7102


def run(coro):
    return asyncio.run(coro)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Script:
    """Answers by what the statement starts with; anything else fails the test.
    Records every write so a refusal can be shown to have written nothing."""

    def __init__(self, rows=None, vals=None):
        self.rows = rows or {}          # prefix -> row (or callable(args) -> row)
        self.vals = vals or {}          # prefix -> value (or callable(args) -> value)
        self.writes: list[str] = []
        self.sql: list[str] = []

    def transaction(self):
        return _Tx()

    def is_in_transaction(self):
        return True

    def _answer(self, table, kind, sql, args):
        s = _norm(sql)
        self.sql.append(s)
        for prefix, v in table.items():
            if s.startswith(prefix):
                return v(args) if callable(v) else v
        raise AssertionError(f"unexpected {kind}: {s[:110]}")

    async def fetchrow(self, sql, *args):
        s = _norm(sql)
        if s.startswith(("INSERT", "UPDATE")):
            self.writes.append(s)
        return self._answer(self.rows, "fetchrow", sql, args)

    async def fetchval(self, sql, *args):
        return self._answer(self.vals, "fetchval", sql, args)

    async def fetch(self, sql, *args):
        return self._answer(self.rows, "fetch", sql, args)

    async def execute(self, sql, *args):
        s = _norm(sql)
        self.sql.append(s)
        if not s.startswith("SELECT pg_advisory_xact_lock"):
            self.writes.append(s)
        return "UPDATE 1"


# ── one epsilon ────────────────────────────────────────────────────────────

def test_the_send_on_epsilon_is_half_a_gram_the_box_rule():
    assert jcv2.SEND_ON_EPS_KG == fg_from_boxes.SAME_KG == 0.0005


@pytest.mark.parametrize("produced, sent, left", [
    (100.0, 60.0, 40.0),
    (100.001, 100.0, 0.001),       # a gram is something to send on
    (100.0004, 100.0, 0.0),        # under half a gram is nothing
    (12.346, 0, 12.346),
    (100.0, 100.0, 0.0),
    (99.0, 100.0, 0.0),            # never below nothing
    (0.1 + 0.2, 0.3, 0.0),         # float noise
    (None, None, 0.0),
])
def test_kg_left_to_send(produced, sent, left):
    assert jcv2.kg_left_to_send(produced, sent) == left


# ── dispatch to next: always capped ────────────────────────────────────────

def _src(dispatched=0.0, status="completed"):
    return {"job_card_id": CARD, "next_job_card_id": NEXT, "planned_qty_kg": 100,
            "dispatched_to_next_kg": dispatched, "output_kind": "WIP", "status": status,
            "entity": "cfpl", "output_code": None, "fg_sku_name": "Roasted Almonds"}


def _dispatch_conn(*, produced, dispatched=0.0):
    def card(args):
        if args[0] == CARD:
            return _src(dispatched)
        return {"status": "locked", "locked_reason": "awaiting_previous_stage", "deleted_at": None}
    return _Script(
        rows={"SELECT job_card_id, next_job_card_id, planned_qty_kg": card,
              "SELECT status, locked_reason, deleted_at FROM job_card_v2": card,
              "INSERT INTO job_card_partial_dispatch_v2":
                  lambda a: {"dispatch_id": a[0], "from_job_card_id": a[1], "to_job_card_id": a[2],
                             "qty_kg": a[3]}},
        vals={"SELECT COALESCE(SUM(output_qty_kg), 0) FROM job_card_output_v2": produced},
    )


@pytest.fixture
def no_wip(monkeypatch):
    async def _materialise(conn, **kw):
        return None
    monkeypatch.setattr(jcv2, "materialise_wip_dispatch", _materialise)


def test_a_dispatch_before_any_output_is_saved_is_refused(no_wip):
    conn = _dispatch_conn(produced=0)
    out = run(jcv2.dispatch_to_next(conn, job_card_id=CARD, qty_kg=25.0, dispatched_by="Ravi K"))
    assert out["error"] == "no_output"
    assert "Save this stage's output before dispatching" in out["message"]
    assert conn.writes == []                       # no dispatch row, no WIP, no carried kg


def test_the_cap_is_output_minus_what_was_already_sent(no_wip):
    conn = _dispatch_conn(produced=100.0, dispatched=60.0)
    out = run(jcv2.dispatch_to_next(conn, job_card_id=CARD, qty_kg=40.001))
    assert out["error"] == "over_dispatch"
    assert out["remaining_kg"] == 40.0
    assert "40.000 kg or less" in out["message"]
    assert conn.writes == []

    ok = run(jcv2.dispatch_to_next(_dispatch_conn(produced=100.0, dispatched=60.0),
                                   job_card_id=CARD, qty_kg=40.0))
    assert ok["dispatched"] is True


def test_the_screens_prefill_of_a_three_decimal_remainder_is_accepted(no_wip):
    """12.346 kg left: the old toFixed(2) default (12.35) was refused."""
    conn = _dispatch_conn(produced=12.346)
    assert run(jcv2.dispatch_to_next(conn, job_card_id=CARD, qty_kg=12.346))["dispatched"] is True
    assert run(jcv2.dispatch_to_next(_dispatch_conn(produced=12.346), job_card_id=CARD,
                                     qty_kg=12.35))["error"] == "over_dispatch"


def test_the_dispatched_qty_is_kept_to_the_gram(no_wip):
    conn = _dispatch_conn(produced=50.0)
    out = run(jcv2.dispatch_to_next(conn, job_card_id=CARD, qty_kg=20.0004))
    assert out["dispatch"]["qty_kg"] == 20.0
    # Under half a gram is no dispatch at all (the table's CHECK qty_kg > 0).
    assert run(jcv2.dispatch_to_next(_dispatch_conn(produced=50.0), job_card_id=CARD,
                                     qty_kg=0.0004))["error"] == "invalid_qty"


def test_everything_already_sent_says_so(no_wip):
    out = run(jcv2.dispatch_to_next(_dispatch_conn(produced=80.0, dispatched=80.0),
                                    job_card_id=CARD, qty_kg=1.0))
    assert out["error"] == "over_dispatch"
    assert out["message"].startswith(f"Everything job card {CARD} made (80.000 kg)")


# ── close: completed cards only, send-on under the lock ────────────────────

def _close_conn(status, *, produced=150.0, dispatched=100.0, signed=True, open_batch=None):
    card = {"job_card_id": CARD, "status": status, "plan_id": 9, "next_job_card_id": NEXT,
            "output_kind": "WIP", "dispatched_to_next_kg": dispatched, "process_group_id": None}
    return _Script(
        rows={"SELECT job_card_id, status, plan_id, next_job_card_id": card,
              "SELECT batch_id, batch_number, COALESCE(NULLIF(btrim(batch_label)": open_batch,
              "SELECT role FROM job_card_sign_off_v2":
                  [{"role": "production_head"}] if signed else [],
              "UPDATE job_card_v2 SET status = 'closed'": {**card, "status": "closed"}},
        vals={"SELECT COUNT(*) FROM job_card_shift_log_v2": 0,
              "SELECT COALESCE(SUM(output_qty_kg), 0) FROM job_card_output_v2": produced},
    )


@pytest.fixture
def sends(monkeypatch):
    log = []

    async def _to_next(conn, *, job_card_id, qty_kg, dispatched_by=None, notes=None, **kw):
        log.append((job_card_id, qty_kg))
        return {"dispatched": True, "unlocked_job_card_id": NEXT}
    monkeypatch.setattr(jcv2, "dispatch_to_next", _to_next)
    return log


@pytest.mark.parametrize("status", ["in_progress", "locked", "unlocked", "material_received"])
@pytest.mark.parametrize("allow_partial", [False, True])
def test_a_card_that_is_not_completed_cannot_be_closed(sends, status, allow_partial):
    """Admins' allow_partial skips the sign-off, never COMPLETE's batch and balance gates."""
    conn = _close_conn(status)
    out = run(jcv2.close_job_card(conn, job_card_id=CARD, allow_partial=allow_partial))
    assert out["error"] == "not_completed"
    assert out["message"].startswith("Complete this job card before closing it")
    assert conn.writes == [] and sends == []


def test_a_completed_card_closes_and_sends_on_what_is_left(sends):
    conn = _close_conn("completed")
    out = run(jcv2.close_job_card(conn, job_card_id=CARD, closed_by="Ravi K"))
    assert out["closed"] is True
    assert sends == [(CARD, 50.0)]
    assert out["unlocked_job_card_ids"] == [NEXT]
    # The card is locked before its status is checked; the send-on uses that row.
    assert conn.sql[0].startswith("SELECT job_card_id, status, plan_id") and conn.sql[0].endswith("FOR UPDATE")
    assert not any(s.startswith("SELECT next_job_card_id, output_kind") for s in conn.sql)


class _Ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def test_the_close_route_refuses_a_card_that_is_not_completed_with_words(monkeypatch):
    from types import SimpleNamespace

    async def refuse(conn, **kw):
        return {"error": "not_completed", "current_status": "in_progress",
                "message": f"Complete this job card before closing it: job card {CARD} is still in progress."}

    async def in_scope(conn, **kw):
        return None
    monkeypatch.setattr(jcv2, "close_job_card", refuse)
    monkeypatch.setattr(PR, "_assert_jc_writable_by_user", in_scope)
    conn = SimpleNamespace(transaction=lambda: _Ctx(None))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        db_pool=SimpleNamespace(acquire=lambda: _Ctx(conn)))))
    admin = SimpleNamespace(full_name="Admin A", phone=None, is_admin=True)
    with pytest.raises(HTTPException) as e:
        run(PR.close_job_card_v2(req, CARD, SimpleNamespace(add_task=None),
                                 PR.CloseJobCardV2Request(allow_partial=True), user=admin))
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "not_completed"
    assert e.value.detail["message"].startswith("Complete this job card before closing it")


def test_closed_and_cancelled_cards_are_still_terminal(sends):
    for status in ("closed", "cancelled"):
        assert run(jcv2.close_job_card(_close_conn(status), job_card_id=CARD))["error"] == "terminal_state"


# ── batches on a finished card ─────────────────────────────────────────────

@pytest.fixture
def unlocked(monkeypatch):
    async def _not_locked(conn, job_card_id):
        return None
    monkeypatch.setattr(batch_svc, "assert_not_locked", _not_locked)


@pytest.mark.parametrize("status", ["completed", "closed", "cancelled"])
def test_no_batch_opens_on_a_finished_card(unlocked, status):
    conn = _Script(rows={"SELECT job_card_id, status FROM job_card_v2":
                         {"job_card_id": CARD, "status": status}})
    out = run(batch_svc.open_batch(conn, job_card_id=CARD))
    assert out["error"] == "job_card_finished" and out["status"] == status
    assert f"Job card {CARD} is {status}" in out["message"]
    assert conn.writes == []


def _batch_close_conn(*, status="in_progress", dispatched=0.0, other_kg=0.0):
    return _Script(
        rows={"SELECT batch_id, job_card_id, batch_number, status, notes":
                  {"batch_id": 55, "job_card_id": CARD, "batch_number": 2, "status": "open",
                   "notes": None},
              "SELECT job_card_id, next_job_card_id, output_kind":
                  {"job_card_id": CARD, "next_job_card_id": NEXT, "output_kind": "WIP", "uom": "kg",
                   "status": status, "dispatched_to_next_kg": dispatched, "entity": "cfpl",
                   "output_code": None, "fg_sku_name": "Roasted Almonds", "next_live": True},
              "SELECT COUNT(*) AS boxes": {"boxes": 0, "kg": 0},
              "SELECT is_balanced, balance_difference_qty, total_input_qty":
                  {"is_balanced": True, "balance_difference_qty": 0, "total_input_qty": 60},
              "UPDATE job_card_batch_v2": {"batch_id": 55, "status": "closed"},
              "INSERT INTO job_card_output_v2": {"output_id": 1, "job_card_id": CARD},
              "INSERT INTO job_card_partial_dispatch_v2":
                  lambda a: {"dispatch_id": a[0], "from_job_card_id": a[1], "to_job_card_id": a[2],
                             "qty_kg": a[3], "notes": a[7]}},
        vals={"SELECT COALESCE(SUM(output_qty_kg), 0) FROM job_card_output_v2": other_kg},
    )


def _close_batch(conn, monkeypatch, produced, **kw):
    async def _resolved(conn, batch_id):
        return CARD
    monkeypatch.setattr(batch_svc, "_lock_check_via_batch", _resolved)
    return run(batch_svc.close_batch(conn, batch_id=55, job_card_id=CARD, produced_qty_kg=produced,
                                     closed_by="Ravi K", **kw))


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_a_batch_on_a_closed_card_cannot_be_completed(monkeypatch, no_wip, status):
    conn = _batch_close_conn(status=status)
    out = _close_batch(conn, monkeypatch, 50.0)
    assert out["error"] == "job_card_finished"
    assert conn.writes == []


def test_a_completed_card_may_still_complete_its_batch(monkeypatch, no_wip):
    """complete_job_card refuses open batches, but one left from before still closes."""
    out = _close_batch(_batch_close_conn(status="completed"), monkeypatch, 50.0)
    assert out["closed"] is True


def test_the_batch_close_does_not_send_twice_what_a_dispatch_already_sent(monkeypatch, no_wip):
    """Saved 50 kg, Dispatched 50 kg while the batch was open, then completed the
    batch on the same 50 kg: nothing more goes."""
    conn = _batch_close_conn(dispatched=50.0)
    out = _close_batch(conn, monkeypatch, 50.0)
    assert out["closed"] is True
    assert out["dispatch"] is None
    assert not any("INSERT INTO job_card_partial_dispatch_v2" in w for w in conn.writes)


def test_the_batch_close_sends_only_what_is_left(monkeypatch, no_wip):
    conn = _batch_close_conn(dispatched=50.0)
    out = _close_batch(conn, monkeypatch, 60.0)
    assert out["dispatch"]["qty_kg"] == 10.0
    assert "the rest was already sent on" in out["dispatch"]["notes"]


def test_the_batch_close_cannot_take_output_below_what_was_sent(monkeypatch, no_wip):
    conn = _batch_close_conn(dispatched=50.0, other_kg=0.0)
    out = _close_batch(conn, monkeypatch, 40.0)
    assert out["error"] == "output_below_dispatched"
    assert conn.writes == []


def test_nothing_sent_yet_reads_no_more(monkeypatch, no_wip):
    conn = _batch_close_conn(dispatched=0.0)
    out = _close_batch(conn, monkeypatch, 50.0)
    assert out["dispatch"]["qty_kg"] == 50.0
    assert not any(s.startswith("SELECT COALESCE(SUM(output_qty_kg)") for s in conn.sql)


# ── output can't go below what was sent on ─────────────────────────────────

def test_the_refusal_names_both_kg():
    out = jcv2.output_below_dispatched_refusal(CARD, dispatched=50.0, other_kg=20.0, batch_kg=25.0)
    assert out["error"] == "output_below_dispatched"
    assert (out["dispatched_kg"], out["output_kg"], out["least_batch_kg"]) == (50.0, 45.0, 30.0)
    assert out["message"] == (f"Job card {CARD} has already sent 50.000 kg on to the next stage, "
                              "so its output can't go down to 45.000 kg. Enter at least 30.000 kg "
                              "for this batch.")
    assert jcv2.output_below_dispatched_refusal(CARD, dispatched=50.0, other_kg=20.0, batch_kg=30.0) is None
    # Raising output (fg_from_boxes only raises) is never refused.
    assert jcv2.output_below_dispatched_refusal(CARD, dispatched=50.0, other_kg=0, batch_kg=50.4) is None


def _output_conn(*, dispatched, other_kg):
    return _Script(
        rows={"SELECT job_card_id, output_kind, dispatched_to_next_kg FROM job_card_v2":
                  {"job_card_id": CARD, "output_kind": "WIP", "dispatched_to_next_kg": dispatched},
              "INSERT INTO job_card_output_v2": {"output_id": 1, "job_card_id": CARD}},
        vals={"SELECT COALESCE(SUM(output_qty_kg), 0) FROM job_card_output_v2": other_kg},
    )


def test_record_output_refuses_to_lower_output_below_dispatched(monkeypatch):
    async def _not_locked(conn, job_card_id):
        return None
    monkeypatch.setattr(jcv2, "assert_not_locked", _not_locked)
    conn = _output_conn(dispatched=50.0, other_kg=0.0)
    out = run(jcv2.record_output(conn, job_card_id=CARD, rm_consumed_kg=60.0, output_qty_kg=40.0,
                                 batch_id=55))
    assert out["error"] == "output_below_dispatched"
    assert conn.writes == []
    # The other batches' rows are summed without this batch's own row.
    other = next(s for s in conn.sql if s.startswith("SELECT COALESCE(SUM(output_qty_kg)"))
    assert "COALESCE(batch_id, 0) <> COALESCE($2::bigint, 0)" in other

    ok = run(jcv2.record_output(_output_conn(dispatched=50.0, other_kg=0.0), job_card_id=CARD,
                                rm_consumed_kg=60.0, output_qty_kg=50.0, batch_id=55))
    assert ok["recorded"] is True


def test_the_outputs_route_refuses_before_its_first_write():
    """The admin_override re-save of a closed batch runs this before its UPDATEs."""
    conn = _Script(vals={"SELECT dispatched_to_next_kg FROM job_card_v2": 50.0,
                         "SELECT COALESCE(SUM(output_qty_kg), 0) FROM job_card_output_v2": 10.0})
    body = PR.RecordOutputV2Request(output_qty_kg=30.0, batch_id=55, admin_override=True)
    with pytest.raises(HTTPException) as e:
        run(PR._refuse_output_below_dispatched(conn, CARD, 55, body))
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "output_below_dispatched"
    assert "50.000 kg" in e.value.detail["message"] and "40.000 kg" in e.value.detail["message"]
    assert conn.writes == []


# ── v1 day-end dispatch ────────────────────────────────────────────────────

def _day_end_conn(outputs):
    def card(args):
        jid = args[0]
        if jid not in outputs:
            return None
        return {"job_card_number": f"JC-{jid}", "fg_actual_kg": outputs[jid]}
    return _Script(rows={"SELECT jc.job_card_number, o.fg_actual_kg FROM job_card jc": card})


def test_day_end_dispatch_of_a_card_with_no_output_is_refused_and_writes_nothing():
    conn = _day_end_conn({1: 80.0, 2: None})
    out = run(day_end.bulk_dispatch(conn, [{"job_card_id": 1, "dispatch_qty": 80.0},
                                           {"job_card_id": 2, "dispatch_qty": 10.0}], "cfpl"))
    assert out["error"] == "no_output" and out["job_card_id"] == 2
    assert conn.writes == []                       # card 1 wasn't written either


def test_day_end_dispatch_is_capped_at_the_cards_output():
    out = run(day_end.bulk_dispatch(_day_end_conn({1: 80.0}),
                                    [{"job_card_id": 1, "dispatch_qty": 80.01}], "cfpl"))
    assert out["error"] == "over_dispatch"
    assert "made 80.000 kg" in out["message"]
