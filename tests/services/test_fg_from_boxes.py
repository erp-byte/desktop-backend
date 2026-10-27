"""FG kg comes from the boxes, on the server (decided 2026-10-26/27).

A box-producing stage's batch FG kg is the total of its live SFG boxes once it
has any. Every writer replaces a typed figure with it and says so (an Android /
API save is kept, not refused). Boxes made after the batch was saved and
completed can raise its FG — never lower it — out of its recorded process loss,
so it stays balanced; the extra waits for Dispatch / completion / close. A batch
whose FG can no longer move keeps today's FG + 0.5 kg cap.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import fg_from_boxes as fgb
from app.modules.production.services import jc_accounting_crud as crud
from app.modules.production.services import job_card_batch_v2 as jbs
from app.modules.production.services import sfg_box_service as sfg

JC = 75009889
OPEN, CLOSED, OTHER, CANCELLED = 4242, 4243, 4244, 4245


def run(coro):
    return asyncio.run(coro)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class _Ctx:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _batch(bid, status, *, number=1, label=None, produced=None, fg=None, input_kg=None,
           planned=None, loss=None):
    return {"batch_id": bid, "batch_number": number, "batch_label": label, "status": status,
            "produced_qty_kg": produced, "fg_actual_kg": fg, "input_qty_kg": input_kg,
            "planned_qty_kg": planned, "process_loss_kg": loss, "job_card_id": JC}


class Conn:
    """The rows the rule reads and writes, keyed by what the SQL starts with."""

    def __init__(self, *, kind="SFG", batches=(), boxes=None, acct=None, card=None,
                 box_sums=None):
        self.kind = kind
        self.batches = {b["batch_id"]: b for b in batches}
        self.boxes = boxes or {}                     # batch_id -> (count, kg)
        self.box_sums = box_sums or {}               # batch_id -> live kg (before the write)
        self.acct = acct or {}                       # batch_id -> {output_qty, process_loss_qty}
        self.card = card or {"status": "in_progress", "next_number": "PLAN-7-L1-S2",
                             "next_status": "locked"}
        self.calls: list[tuple[str, str, tuple]] = []

    def is_in_transaction(self):
        return True

    def transaction(self):
        return _Ctx()

    def ran(self, kind, prefix):
        return [c for c in self.calls if c[0] == kind and c[1].startswith(prefix)]

    async def execute(self, sql, *args):
        self.calls.append(("execute", _norm(sql), args))
        return "UPDATE 1"

    async def fetch(self, sql, *args):
        s = _norm(sql)
        self.calls.append(("fetch", s, args))
        if s.startswith("SELECT batch_id, batch_number, batch_label, status"):
            return [self.batches[i] for i in args[1] if i in self.batches]
        if s.startswith("SELECT batch_id FROM job_card_batch_v2"):
            return [{"batch_id": i} for i in args[1] if i in self.batches]
        if s.startswith("SELECT carton_id, batch_id FROM sfg_box"):
            return getattr(self, "updatable", [])
        raise AssertionError(f"unexpected fetch: {s[:100]}")

    async def fetchval(self, sql, *args):
        s = _norm(sql)
        self.calls.append(("fetchval", s, args))
        if s.startswith("SELECT output_qty FROM job_card_accounting_v2"):
            a = self.acct.get(args[1])
            return None if a is None else a["output_qty"]
        if s.startswith("SELECT process_loss_qty FROM job_card_accounting_v2"):
            a = self.acct.get(args[1])
            return None if a is None else a["process_loss_qty"]
        if s.startswith("SELECT COALESCE(SUM(net_weight), 0) FROM sfg_box"):
            return self.box_sums.get(args[0], 0)
        if s.startswith("SELECT COALESCE(MAX(CAST(split_part(carton_id"):
            return 0
        raise AssertionError(f"unexpected fetchval: {s[:100]}")

    async def fetchrow(self, sql, *args):
        s = _norm(sql)
        self.calls.append(("fetchrow", s, args))
        if s.startswith("SELECT COUNT(*) AS boxes"):
            n, kg = self.boxes.get(args[0], (0, 0))
            return {"boxes": n, "kg": kg}
        if s.startswith("SELECT b.status, b.produced_qty_kg, b.fg_actual_kg, j.output_kind"):
            b = self.batches.get(args[0])
            return None if b is None else {**b, "output_kind": self.kind}
        if s.startswith("SELECT j.status, n.job_card_number AS next_number"):
            return self.card
        if s.startswith("SELECT output_qty, process_loss_qty FROM job_card_accounting_v2"):
            return self.acct.get(args[1])
        if s.startswith("UPDATE job_card_accounting_v2"):
            a = self.acct.get(args[1])
            d = args[2]
            return None if a is None else {"output_qty": a["output_qty"] + d,
                                           "process_loss_qty": max(a["process_loss_qty"] - d, 0)}
        if "FROM job_card_v2" in s and s.startswith("SELECT job_card_id, job_card_number, fg_sku_name"):
            return {"job_card_id": JC, "job_card_number": "PLAN-7-L1-S1", "fg_sku_name": "Seeds",
                    "output_kind": self.kind, "output_code": "SFG0001", "entity": "CFPL",
                    "floor": "ROASTING", "stage": "Roasting", "status": "in_progress"}
        if s.startswith("INSERT INTO sfg_box"):
            return {"box_id": args[0], "net_weight": args[8], "batch_id": args[15]}
        if s.startswith("INSERT INTO job_card_remarks_v2"):
            return {"remark_id": args[0]}
        raise AssertionError(f"unexpected fetchrow: {s[:100]}")


# ── which figure a write must carry ───────────────────────────────────────────

def test_a_stage_that_does_not_box_keeps_the_typed_figure():
    conn = Conn(kind="FG", batches=[_batch(OPEN, "open")], boxes={OPEN: (3, 29.4)})
    assert run(fgb.fg_for_write(conn, JC, OPEN)) is None


def test_a_batch_with_no_boxes_keeps_the_typed_figure():
    conn = Conn(batches=[_batch(OPEN, "open")])
    assert run(fgb.fg_for_write(conn, JC, OPEN)) is None
    assert run(fgb.fg_for_write(conn, JC, None)) is None


def test_an_open_batch_takes_its_box_total():
    conn = Conn(batches=[_batch(OPEN, "open")], boxes={OPEN: (3, 29.4)})
    assert run(fgb.fg_for_write(conn, JC, OPEN)) == {"kg": 29.4, "boxes": 3, "box_kg": 29.4,
                                                    "status": "open"}


def test_a_completed_batch_keeps_its_fg_even_when_its_boxes_are_lighter():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, fg=100)], boxes={CLOSED: (5, 98.2)})
    got = run(fgb.fg_for_write(conn, JC, CLOSED))
    assert got["kg"] == 100.0 and got["box_kg"] == 98.2


def test_a_typed_figure_is_replaced_logged_and_reported():
    conn = Conn(batches=[_batch(OPEN, "open")], boxes={OPEN: (3, 29.4)})
    kg, note = run(fgb.take_fg(conn, job_card_id=JC, batch_id=OPEN, typed=30.0, actor="Floor Fay"))
    assert kg == 29.4
    assert note == {"kg": 29.4, "typed_kg": 30.0, "boxes": 3,
                    "message": "FG kg comes from this batch's 3 boxes: saved 29.400 kg instead "
                               "of the 30.000 kg sent."}
    log = [c for c in conn.calls if c[0] == "execute" and "amendment_log" in c[1]]
    assert log and log[0][2][2] == f"fg_from_boxes:{OPEN}.output_qty_kg"


def test_the_same_figure_is_not_reported_or_logged():
    conn = Conn(batches=[_batch(OPEN, "open")], boxes={OPEN: (1, 29.4)})
    assert run(fgb.take_fg(conn, job_card_id=JC, batch_id=OPEN, typed=29.4002, actor="x")) == (29.4, None)
    assert not [c for c in conn.calls if "amendment_log" in c[1]]


def test_a_batch_without_boxes_hands_the_typed_figure_back():
    conn = Conn(batches=[_batch(OPEN, "open")])
    assert run(fgb.take_fg(conn, job_card_id=JC, batch_id=OPEN, typed=12.5, actor="x")) == (12.5, None)


def test_one_box_reads_as_one_box():
    assert fgb.replacement(None, {"kg": 9.8, "boxes": 1})["message"] == (
        "FG kg comes from this batch's 1 box: saved 9.800 kg.")


# ── completing on stale accounting ────────────────────────────────────────────

def test_completion_waits_for_accounting_saved_on_the_boxes():
    conn = Conn(acct={OPEN: {"output_qty": 28.0, "process_loss_qty": 1.0}})
    refusal = run(fgb.stale_accounting(conn, job_card_id=JC, batch_id=OPEN, box_kg=29.4))
    assert refusal["error"] == "accounting_stale"
    assert refusal["message"] == ("This batch's boxes total 29.400 kg, but its accounting was saved "
                                  "with 28.000 kg. Save the batch again so it balances on the boxes, "
                                  "then complete it.")
    conn.acct[OPEN]["output_qty"] = 29.4
    assert run(fgb.stale_accounting(conn, job_card_id=JC, batch_id=OPEN, box_kg=29.4)) is None
    assert run(fgb.stale_accounting(Conn(), job_card_id=JC, batch_id=OPEN, box_kg=29.4)) is None


# ── a box write ───────────────────────────────────────────────────────────────

def _check(conn, totals, moved=()):
    return run(fgb.check_box_change(conn, job_card_id=JC, totals_after=totals,
                                    moved_out_of=set(moved), tolerance_kg=0.5))


def test_an_open_batch_is_bounded_only_by_what_it_was_opened_with():
    assert _check(Conn(batches=[_batch(OPEN, "open")]), {OPEN: 500}) == (None, [])
    refusal, _ = _check(Conn(batches=[_batch(OPEN, "open", number=2, input_kg=40)]), {OPEN: 40.6})
    assert refusal == {"error": "batch_over_capacity",
                       "message": "Boxes for Batch 2 would total 40.600 kg, over the 40.000 kg it "
                                  "was opened with."}
    assert _check(Conn(batches=[_batch(OPEN, "open", planned=40)]), {OPEN: 40.4})[0] is None


def test_a_cancelled_batch_takes_no_boxes():
    refusal, _ = _check(Conn(batches=[_batch(CANCELLED, "cancelled", label="Lot A")]), {CANCELLED: 5})
    assert refusal["error"] == "batch_cancelled"
    assert refusal["message"].startswith("Lot A is cancelled")


def test_boxes_after_the_save_raise_a_completed_batchs_fg_out_of_its_loss():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, fg=100, loss=2)],
                acct={CLOSED: {"output_qty": 100, "process_loss_qty": 2.0}})
    refusal, raises = _check(conn, {CLOSED: 101.2})
    assert refusal is None
    assert raises == [{"batch_id": CLOSED, "batch": "Batch 1", "from_kg": 100.0, "to_kg": 101.2,
                       "raise_kg": 1.2}]


def test_the_raise_stops_at_the_loss_and_the_scale_slack_is_kept():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=2)],
                acct={CLOSED: {"output_qty": 100, "process_loss_qty": 2.0}})
    refusal, raises = _check(conn, {CLOSED: 102.4})
    assert refusal is None and raises[0]["raise_kg"] == 2.0 and raises[0]["to_kg"] == 102.0
    refusal, _ = _check(conn, {CLOSED: 102.6})
    assert refusal["error"] == "batch_over_capacity"
    assert refusal["message"] == ("Boxes for Batch 1 would total 102.600 kg: more than its FG "
                                  "100.000 kg plus its recorded process loss 2.000 kg. Check the box "
                                  "weights, or correct the batch's accounting.")


def test_with_no_saved_accounting_the_batch_rows_loss_counts():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=1.5)])
    assert _check(conn, {CLOSED: 101.0})[1][0]["raise_kg"] == 1.0


def test_lighter_or_equal_boxes_leave_fg_alone():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=2)])
    assert _check(conn, {CLOSED: 98.0}) == (None, [])
    assert _check(conn, {CLOSED: 100.0003}) == (None, [])


def test_fg_cannot_move_once_this_card_is_closed():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=2)],
                card={"status": "closed", "next_number": "PLAN-7-L1-S2", "next_status": "in_progress"})
    assert _check(conn, {CLOSED: 100.4}) == (None, [])          # today's slack, no raise
    refusal, _ = _check(conn, {CLOSED: 101.0})
    assert refusal["message"].endswith("Its FG can't go up any more: this job card is closed.")


def test_fg_cannot_move_once_the_next_stage_has_finished():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=2)],
                card={"status": "completed", "next_number": "PLAN-7-L1-S2", "next_status": "completed"})
    refusal, raises = _check(conn, {CLOSED: 101.0})
    assert raises == []
    assert refusal["message"].endswith(
        "Its FG can't go up any more: the next stage's job card PLAN-7-L1-S2 is already completed.")


def test_a_completed_card_with_a_next_stage_still_running_can_be_raised():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=2)],
                card={"status": "completed", "next_number": "PLAN-7-L1-S2", "next_status": "in_progress"})
    assert _check(conn, {CLOSED: 101.0})[1][0]["raise_kg"] == 1.0


def test_a_box_cannot_leave_a_completed_batch():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100), _batch(OPEN, "open", number=2)])
    refusal, _ = _check(conn, {OPEN: 10}, moved=[CLOSED])
    assert refusal["error"] == "box_in_completed_batch"


def test_the_batch_rows_are_locked_in_id_order_before_judging():
    conn = Conn(batches=[_batch(OPEN, "open"), _batch(CLOSED, "closed", produced=1)])
    _check(conn, {CLOSED: 1, OPEN: 1})
    (_, sql, args), = conn.ran("fetch", "SELECT batch_id, batch_number")
    assert sql.endswith("ORDER BY batch_id FOR UPDATE") and args == (JC, [OPEN, CLOSED])


def test_a_raise_moves_kg_from_loss_to_fg_on_all_three_rows_and_says_so():
    conn = Conn(acct={CLOSED: {"output_qty": 100, "process_loss_qty": 2.0}})
    done = run(fgb.apply_raises(conn, job_card_id=JC, actor="Floor Fay", raises=[
        {"batch_id": CLOSED, "batch": "Batch 1", "from_kg": 100.0, "to_kg": 101.2, "raise_kg": 1.2}]))
    batch_upd = conn.ran("execute", "UPDATE job_card_batch_v2")
    output_upd = conn.ran("execute", "UPDATE job_card_output_v2")
    acct_upd = conn.ran("fetchrow", "UPDATE job_card_accounting_v2")
    assert batch_upd[0][2] == (CLOSED, 1.2)
    assert "process_loss_kg = GREATEST(COALESCE(process_loss_kg, 0) - $2, 0)" in batch_upd[0][1]
    assert output_upd[0][2] == (JC, CLOSED, 1.2)
    assert acct_upd[0][2] == (JC, CLOSED, 1.2)
    # The edit log shows the two accounting figures that moved.
    logged = {c[2][2]: (c[2][3], c[2][4]) for c in conn.calls if "amendment_log" in c[1]}
    assert logged[f"accounting:{CLOSED}.output_qty"] == ("100", "101.2")
    assert logged[f"accounting:{CLOSED}.process_loss_qty"] == ("2.0", "0.8")
    assert conn.ran("fetchrow", "INSERT INTO job_card_remarks_v2")
    assert done[0]["message"] == "Batch 1: FG raised to 101.200 kg by its boxes (process loss 1.200 kg lower)."
    # Nothing is sent on from a box write.
    assert not [c for c in conn.calls if "partial_dispatch" in c[1] or "dispatched_to_next_kg" in c[1]]


# ── the box routes' services ──────────────────────────────────────────────────

def test_boxing_after_the_save_is_no_longer_capped_at_the_saved_fg():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=2)],
                acct={CLOSED: {"output_qty": 100, "process_loss_qty": 2.0}},
                box_sums={CLOSED: 95.0})
    out = run(sfg.create_wip_boxes(conn, JC, [{"net_weight": 6.2, "batch_id": CLOSED}],
                                   created_by="Floor Fay"))
    assert "error" not in out
    assert out["fg_raised"][0]["to_kg"] == 101.2
    # Judged before the box was written.
    order = [c[1][:30] for c in conn.calls]
    assert order.index(next(o for o in order if o.startswith("SELECT batch_id, batch_number"))) < \
        order.index(next(o for o in order if o.startswith("INSERT INTO sfg_box")))


def test_a_cancelled_box_is_left_out_of_the_batch_total():
    conn = Conn(batches=[_batch(OPEN, "open", input_kg=10)], box_sums={OPEN: 0})
    out = run(sfg.create_wip_boxes(conn, JC, [{"net_weight": 11, "batch_id": OPEN}]))
    assert out["error"] == "batch_over_capacity" and "11.000 kg" in out["message"]
    (_, sql, args), = conn.ran("fetchval", "SELECT COALESCE(SUM(net_weight), 0) FROM sfg_box")
    assert "status IS DISTINCT FROM $2" in sql and args[1] == "CANCELLED"


def test_a_refused_box_write_writes_nothing():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100, loss=0)], box_sums={CLOSED: 100})
    out = run(sfg.create_wip_boxes(conn, JC, [{"net_weight": 5, "batch_id": CLOSED}]))
    assert out["error"] == "batch_over_capacity"
    assert not conn.ran("fetchrow", "INSERT INTO sfg_box")
    assert not conn.ran("execute", "UPDATE")


def test_an_edit_that_moves_a_box_out_of_a_completed_batch_is_refused():
    conn = Conn(batches=[_batch(CLOSED, "closed", produced=100), _batch(OPEN, "open", number=2)])
    conn.updatable = [{"carton_id": "48213307-1", "batch_id": CLOSED}]
    out = run(sfg.update_wip_boxes(conn, JC, [{"box_id": "48213307-1", "net_weight": 9.8,
                                               "batch_id": OPEN}], changed_by="Floor Fay"))
    assert out["error"] == "box_in_completed_batch"
    assert conn.calls[0][1] == "SELECT pg_advisory_xact_lock($1)"          # the card's box lock first
    assert not conn.ran("fetchrow", "UPDATE sfg_box")


# ── the writers ───────────────────────────────────────────────────────────────

def _body(**kw):
    return PR.RecordOutputV2Request(**kw)


def test_the_outputs_route_puts_the_box_total_in_both_spellings():
    conn = Conn(batches=[_batch(OPEN, "open")], boxes={OPEN: (3, 29.4)})
    body = _body(fg_actual_kg=31.0)                        # Android's legacy spelling
    note = run(PR._fg_from_boxes_into(conn, JC, OPEN, body, "Floor Fay"))
    assert body.output_qty_kg == 29.4 and body.fg_actual_kg == 29.4
    assert note["typed_kg"] == 31.0


def test_a_save_with_no_fg_and_no_completion_is_left_alone():
    conn = Conn(batches=[_batch(OPEN, "open")], boxes={OPEN: (3, 29.4)})
    body = _body(notes="consumption only")
    assert run(PR._fg_from_boxes_into(conn, JC, OPEN, body, "x")) is None
    assert body.output_qty_kg is None and conn.calls == []


def test_completing_without_an_fg_figure_completes_on_the_boxes():
    conn = Conn(batches=[_batch(OPEN, "open")], boxes={OPEN: (2, 19.6)})
    body = _body(complete_batch=True)
    run(PR._fg_from_boxes_into(conn, JC, OPEN, body, "x"))
    assert body.output_qty_kg == 19.6


class _SummaryConn(Conn):
    def __init__(self, owner, **kw):
        super().__init__(**kw)
        self.owner = owner

    async def fetchval(self, sql, *args):
        if _norm(sql).startswith("SELECT job_card_id FROM job_card_batch_v2"):
            return self.owner
        return await super().fetchval(sql, *args)


def _summary(monkeypatch, conn, **body):
    from app.modules.production.services import jc_accounting_v2
    seen = {}

    async def save(conn, *, job_card_id, payload, saved_by, batch_id):
        seen["payload"] = payload
        return {"saved": True}
    monkeypatch.setattr(jc_accounting_v2, "save_accounting", save)
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    user = SimpleNamespace(full_name="Floor Fay", phone=None)
    out = run(PR.save_accounting_summary_v2(req, JC, PR.AccountingSummaryRequest(**body), user=user))
    return out, seen.get("payload")


def test_the_summary_balances_on_the_boxes(monkeypatch):
    conn = _SummaryConn(JC, batches=[_batch(OPEN, "open")], boxes={OPEN: (3, 29.4)})
    out, payload = _summary(monkeypatch, conn, batch_id=OPEN, output_qty=30.0, total_input_qty=31)
    assert payload["output_qty"] == 29.4
    assert out["fg_from_boxes"]["typed_kg"] == 30.0


def test_the_summary_refuses_another_cards_batch(monkeypatch):
    conn = _SummaryConn(11111111, batches=[_batch(OPEN, "open")])
    with pytest.raises(HTTPException) as e:
        _summary(monkeypatch, conn, batch_id=OPEN, output_qty=30.0, total_input_qty=31)
    assert e.value.status_code == 400 and e.value.detail["error"] == "batch_jc_mismatch"


class _CloseConn(Conn):
    """close_batch for an open SFG batch with three boxes (29.4 kg)."""

    def __init__(self, saved_output_qty=None):
        super().__init__(batches=[_batch(OPEN, "open")], boxes={OPEN: (3, 29.4)},
                         acct=({OPEN: {"output_qty": saved_output_qty, "process_loss_qty": 0.6}}
                               if saved_output_qty is not None else {}))

    async def fetchrow(self, sql, *args):
        s = _norm(sql)
        if s.startswith("SELECT batch_id, job_card_id, batch_number, status, notes"):
            self.calls.append(("fetchrow", s, args))
            return {"batch_id": OPEN, "job_card_id": JC, "batch_number": 1, "status": "open", "notes": None}
        if s.startswith("SELECT job_card_id, next_job_card_id"):
            return {"job_card_id": JC, "next_job_card_id": None, "output_kind": "SFG", "uom": "kg",
                    "dispatched_to_next_kg": 0, "entity": "cfpl", "output_code": None,
                    "fg_sku_name": "Seeds", "next_live": None}
        if s.startswith("SELECT is_balanced, balance_difference_qty, total_input_qty"):
            return {"is_balanced": True, "balance_difference_qty": 0, "total_input_qty": 30}
        if s.startswith("UPDATE job_card_batch_v2"):
            self.produced = args[1]
            return {"batch_id": OPEN, "job_card_id": JC, "status": "closed"}
        if "INSERT INTO job_card_output_v2" in s:
            self.output = args[4]
            return {"output_id": 1, "job_card_id": JC}
        return await super().fetchrow(sql, *args)


async def _resolved(conn=None, batch_id=None):
    return JC                                       # the batch's card, not locked


def _close(monkeypatch, conn, produced):
    monkeypatch.setattr(jbs, "_lock_check_via_batch", _resolved)
    return run(jbs.close_batch(conn, batch_id=OPEN, job_card_id=JC, produced_qty_kg=produced,
                               is_balanced=True))


def test_closing_a_boxed_batch_records_the_boxes_whatever_was_typed(monkeypatch):
    conn = _CloseConn(saved_output_qty=29.4)
    out = _close(monkeypatch, conn, produced=31.0)
    assert out.get("closed") is True
    assert conn.produced == 29.4 and conn.output == 29.4
    # The card's box lock comes before the batch row lock.
    lock = next(i for i, c in enumerate(conn.calls) if c[1] == "SELECT pg_advisory_xact_lock($1)")
    batch = next(i for i, c in enumerate(conn.calls) if c[1].startswith("SELECT batch_id, job_card_id, batch_number"))
    assert lock < batch


def test_closing_on_accounting_saved_before_a_box_changed_is_refused(monkeypatch):
    conn = _CloseConn(saved_output_qty=28.0)
    out = _close(monkeypatch, conn, produced=28.0)
    assert out["error"] == "accounting_stale"
    assert not conn.ran("fetchrow", "UPDATE job_card_batch_v2")


class _CancelConn(Conn):
    async def fetchval(self, sql, *args):
        if _norm(sql).startswith("SELECT EXISTS"):
            return False                            # no output / dispatch / shift rows
        return await super().fetchval(sql, *args)

    async def fetchrow(self, sql, *args):
        if _norm(sql).startswith("SELECT batch_id, status FROM job_card_batch_v2"):
            return {"batch_id": OPEN, "status": "open"}
        return await super().fetchrow(sql, *args)


def test_a_batch_holding_boxes_cannot_be_cancelled(monkeypatch):
    monkeypatch.setattr(jbs, "_lock_check_via_batch", _resolved)
    conn = _CancelConn(boxes={OPEN: (2, 19.6)})
    out = run(jbs.cancel_batch(conn, batch_id=OPEN, job_card_id=JC))
    assert out["error"] == "batch_has_attached_rows"
    assert out["message"].startswith("Batch has 2 box(es) on Boxes printing")
    assert not conn.ran("fetchrow", "UPDATE job_card_batch_v2")


def test_a_completed_boxed_batchs_record_cannot_be_deleted(monkeypatch):
    async def guard(conn, **kw):
        return {"jc": {"status": "in_progress"}, "batch": {"status": "closed"}}

    async def not_locked(conn, jc_id):
        return None
    monkeypatch.setattr(crud, "_resolve", guard)
    monkeypatch.setattr(crud, "assert_not_locked", not_locked)
    out = run(crud.delete_record(Conn(boxes={CLOSED: (5, 100.0)}), job_card_id=JC, plan_id=1,
                                 batch_id=CLOSED))
    assert out["error"] == "batch_boxed"
