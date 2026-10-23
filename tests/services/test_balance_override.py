"""Unbalanced batches and job cards: who may override, and what people are told.

Rules (agreed with the business, 2026-10-23):
  * the allowed difference is the BOM's tolerance x the batch's input
    (0.1% by design: 100 g on 100 kg), or 0.05 kg when there is no input;
  * when the figures don't balance, an ADMIN or a FLOOR MANAGER may close the
    batch / complete the job card anyway, but only with a typed reason;
  * that override is recorded as a 'deviation' remark on the card, under the
    logged-in user's name, with how far off it was;
  * the message says how far off it is and how much is allowed, in plain words;
  * an open shift no longer blocks completion: completing ends it.

Services and the database are faked.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.modules.production.services import balance_override as bo
from app.modules.production.services.job_card_v2 import complete_job_card


# ── Plain rules ──────────────────────────────────────────────────────────────
def test_allowed_difference_is_the_tolerance_share_of_the_input():
    assert bo.allowed_difference_kg(100, 0.001) == pytest.approx(0.1)
    assert bo.allowed_difference_kg(Decimal("250"), Decimal("0.001")) == pytest.approx(0.25)


def test_allowed_difference_without_input_is_the_fixed_fallback():
    assert bo.allowed_difference_kg(0, 0.001) == pytest.approx(0.05)
    assert bo.allowed_difference_kg(None, None) == pytest.approx(0.05)


def test_missing_tolerance_uses_the_designed_default():
    assert bo.allowed_difference_kg(100, None) == pytest.approx(0.1)


def test_message_says_how_far_off_and_how_much_is_allowed_in_plain_words():
    msg = bo.unbalanced_message(-0.3126, 0.1, what="Batch 2")
    assert "Batch 2" in msg
    assert "off by 0.313 kg" in msg
    assert "0.100 kg is allowed" in msg
    assert "admin or floor manager" in msg
    for jargon in ("force", "request_id", "amendment", "R8", "is_balanced"):
        assert jargon not in msg


def test_a_reason_must_say_something():
    assert bo.clean_reason(None) is None
    assert bo.clean_reason("   ") is None
    assert bo.clean_reason("ok") is None
    assert bo.clean_reason("  Scale was recalibrated  ") == "Scale was recalibrated"


def test_override_remark_records_the_numbers_and_the_reason():
    text = bo.override_remark(-0.3126, 0.1, "Scale was recalibrated", what="Job card")
    assert text.startswith("Job card completed although it doesn't balance")
    assert "off by 0.313 kg" in text and "0.100 kg allowed" in text
    assert text.endswith("Reason: Scale was recalibrated")


# ── Who may override ────────────────────────────────────────────────────────
class _RoleConn:
    def __init__(self, is_floor_manager):
        self.is_floor_manager, self.queries = is_floor_manager, []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        return self.is_floor_manager


@pytest.mark.asyncio
async def test_admin_may_override_without_a_lookup():
    conn = _RoleConn(False)
    assert await bo.can_override_balance(conn, SimpleNamespace(is_admin=True, role_ids=[])) is True
    assert conn.queries == []


@pytest.mark.asyncio
async def test_floor_manager_in_any_of_their_roles_may_override():
    conn = _RoleConn(True)
    user = SimpleNamespace(is_admin=False, role_ids=[7, 3])
    assert await bo.can_override_balance(conn, user) is True
    sql, args = conn.queries[0]
    assert "auth_role" in sql and args[0] == [7, 3] and args[1] == "floor_manager"


@pytest.mark.asyncio
async def test_anyone_else_may_not_override():
    assert await bo.can_override_balance(_RoleConn(False), SimpleNamespace(is_admin=False, role_ids=[5])) is False
    conn = _RoleConn(True)
    assert await bo.can_override_balance(conn, SimpleNamespace(is_admin=False, role_ids=[])) is False
    assert conn.queries == []


# ── complete_job_card ───────────────────────────────────────────────────────
JC = 5101


class _CompleteConn:
    """Answers complete_job_card's reads; records its writes."""

    def __init__(self, *, balanced, open_shift_ids=(), tolerance=Decimal("0.001")):
        self.balanced, self.open_shift_ids, self.tolerance = balanced, list(open_shift_ids), tolerance
        self.stopped_shifts: list[int] = []
        self.update_args = None
        self.remarks: list[tuple] = []

    async def fetch(self, sql, *args):
        if "FROM job_card_shift_log_v2" in sql and "end_at IS NULL" in sql:
            return [{"log_id": i} for i in self.open_shift_ids if i not in self.stopped_shifts]
        return []

    async def fetchrow(self, sql, *args):
        if "SELECT status, start_time, total_time_min" in sql:
            return {"status": "in_progress", "start_time": None, "total_time_min": None}
        if "FROM job_card_shift_log_v2 WHERE log_id" in sql:
            return {"job_card_id": JC, "end_at": None}
        if "UPDATE job_card_shift_log_v2" in sql:
            self.stopped_shifts.append(args[0])
            self.shift_notes_arg = args[2]
            return {"log_id": args[0], "job_card_id": JC}
        if "FROM   job_card_batch_v2" in sql and "status = 'open'" in sql:
            return None
        if "WITH locked AS" in sql:
            return {"is_balanced": self.balanced,
                    "balance_difference_qty": Decimal("-0.3126") if not self.balanced else Decimal("0"),
                    "row_count": 1}
        if "FROM   job_card_accounting_v2 a" in sql:
            return {"balance_difference_qty": Decimal("-0.3126"), "total_input_qty": Decimal("100"),
                    "batch_number": 2}
        if sql.lstrip().startswith("UPDATE job_card_v2"):
            self.update_args = args
            return {"status": "completed", "end_time": None, "total_time_min": 30, "force_closed": args[2]}
        if "INSERT INTO job_card_remarks_v2" in sql:
            self.remarks.append(args)
            return {"remark_id": args[0]}
        return None

    async def fetchval(self, sql, *args):
        if "allowed_balance_tolerance_pct" in sql:
            return self.tolerance
        if "SUM(" in sql and "job_card_shift_log_v2" in sql:
            return 30
        if "SELECT total_time_min FROM job_card_v2" in sql:
            return 30
        return None

    async def execute(self, sql, *args):
        return "UPDATE 1"

    # complete_job_card runs inside the route's transaction; the remark insert
    # (insert_with_pk_retry) checks for it and opens a savepoint.
    def is_in_transaction(self):
        return True

    def transaction(self):
        class _Tx:
            async def __aenter__(_s): return None
            async def __aexit__(_s, *exc): return False
        return _Tx()


@pytest.mark.asyncio
async def test_unbalanced_card_is_refused_with_a_plain_message():
    conn = _CompleteConn(balanced=False)
    out = await complete_job_card(conn, job_card_id=JC, completed_by="Raju Paikrao")
    assert out["error"] == "unbalanced"
    assert out["allowed_difference_kg"] == pytest.approx(0.1)
    assert out["batch_number"] == 2
    assert "off by 0.313 kg" in out["message"] and "0.100 kg is allowed" in out["message"]
    assert "force" not in out["message"]
    assert conn.update_args is None and conn.remarks == []


@pytest.mark.asyncio
async def test_a_floor_manager_completes_it_anyway_with_a_reason():
    conn = _CompleteConn(balanced=False)
    out = await complete_job_card(conn, job_card_id=JC, completed_by="Raju Paikrao",
                                  override_allowed=True, override_reason="Scale was recalibrated")
    assert out["completed"] is True
    # Stamped as a forced close, by the LOGGED-IN user, with no amendment id.
    assert conn.update_args[2] is True            # force_closed
    assert conn.update_args[3] is None            # force_close_request_id
    assert conn.update_args[4] == "Raju Paikrao"  # force_close_by
    # And the why is on the card for everyone to see.
    (remark,) = conn.remarks
    assert remark[1] == JC and remark[2] == "deviation" and remark[4] == "Raju Paikrao"
    assert "off by 0.313 kg" in remark[3] and remark[3].endswith("Reason: Scale was recalibrated")


@pytest.mark.asyncio
async def test_a_too_short_reason_is_refused():
    conn = _CompleteConn(balanced=False)
    out = await complete_job_card(conn, job_card_id=JC, completed_by="Raju Paikrao",
                                  override_allowed=True, override_reason="ok")
    assert out["error"] == "override_reason_required"
    assert conn.update_args is None


@pytest.mark.asyncio
async def test_pressing_complete_without_a_reason_shows_how_far_off():
    """Completing has no separate override flag: only a typed reason is an
    override attempt. A plain COMPLETE from an admin / floor manager gets the
    'off by / allowed' message (the screen then offers the reason box)."""
    for blank in (None, "", "   "):
        conn = _CompleteConn(balanced=False)
        out = await complete_job_card(conn, job_card_id=JC, completed_by="Raju Paikrao",
                                      override_allowed=True, override_reason=blank)
        assert out["error"] == "unbalanced", blank
        assert "off by 0.313 kg" in out["message"]
        assert conn.update_args is None


@pytest.mark.asyncio
async def test_a_reason_from_someone_not_allowed_is_ignored():
    conn = _CompleteConn(balanced=False)
    out = await complete_job_card(conn, job_card_id=JC, completed_by="Somebody",
                                  override_allowed=False, override_reason="Please let me")
    assert out["error"] == "unbalanced"
    assert conn.update_args is None and conn.remarks == []


@pytest.mark.asyncio
async def test_a_balanced_card_completes_without_a_remark():
    conn = _CompleteConn(balanced=True)
    out = await complete_job_card(conn, job_card_id=JC, completed_by="Raju Paikrao",
                                  override_allowed=True, override_reason="Not needed here")
    assert out["completed"] is True
    assert conn.update_args[2] is False and conn.remarks == []


@pytest.mark.asyncio
async def test_an_open_shift_is_ended_instead_of_blocking():
    conn = _CompleteConn(balanced=True, open_shift_ids=[41])
    out = await complete_job_card(conn, job_card_id=JC, completed_by="Raju Paikrao")
    assert out["completed"] is True
    assert conn.stopped_shifts == [41]
    # stop_shift REPLACES the segment's notes when given any (COALESCE($3, notes)),
    # so ending it must pass none — the operator's own notes stay.
    assert conn.shift_notes_arg is None


# ── Closing one batch ───────────────────────────────────────────────────────
class _BatchConn:
    def __init__(self, acct, tolerance=Decimal("0.001")):
        self.acct, self.tolerance = acct, tolerance

    async def fetchrow(self, sql, *args):
        return self.acct if "FROM   job_card_accounting_v2" in sql else None

    async def fetchval(self, sql, *args):
        return self.tolerance if "allowed_balance_tolerance_pct" in sql else None


def _check(conn, **kw):
    args = dict(job_card_id=JC, batch_id=9, batch_number=3, ui_is_balanced=None,
                ui_difference_kg=None, allow_unbalanced=False)
    args.update(kw)
    return bo.batch_balance_check(conn, **args)


UNBAL = {"is_balanced": False, "balance_difference_qty": Decimal("0.5"), "total_input_qty": Decimal("250")}


@pytest.mark.asyncio
async def test_a_balanced_batch_closes():
    ok = {"is_balanced": True, "balance_difference_qty": Decimal("0.1"), "total_input_qty": Decimal("250")}
    assert await _check(_BatchConn(ok)) == (None, None)


@pytest.mark.asyncio
async def test_an_unbalanced_batch_is_refused_with_its_numbers():
    refusal, override = await _check(_BatchConn(UNBAL))
    assert override is None
    assert refusal["error"] == "batch_unbalanced"
    assert refusal["allowed_difference_kg"] == pytest.approx(0.25)   # 0.1% of 250 kg
    assert refusal["message"].startswith("Batch 3 doesn't balance: it is off by 0.500 kg, and 0.250 kg is allowed")


@pytest.mark.asyncio
async def test_the_saved_accounting_outranks_the_screen():
    refusal, _ = await _check(_BatchConn(UNBAL), ui_is_balanced=True, ui_difference_kg=0.0)
    assert refusal["error"] == "batch_unbalanced"


@pytest.mark.asyncio
async def test_an_override_lets_it_close_and_reports_how_far_off():
    refusal, override = await _check(_BatchConn(UNBAL), allow_unbalanced=True)
    assert refusal is None
    assert override == {"difference_kg": 0.5, "allowed_kg": pytest.approx(0.25)}


@pytest.mark.asyncio
async def test_nothing_saved_yet_asks_for_the_accounting_first():
    refusal, _ = await _check(_BatchConn(None), ui_is_balanced=False, ui_difference_kg=2.0)
    assert refusal["error"] == "batch_unbalanced"
    assert "Save the Accounting figures first" in refusal["message"]
    assert "allowed" not in refusal["message"]


@pytest.mark.asyncio
async def test_nothing_saved_but_the_screen_says_balanced_closes():
    assert await _check(_BatchConn(None), ui_is_balanced=True) == (None, None)


def test_an_override_without_a_known_allowance_still_reads_well():
    text = bo.override_remark(2.0, None, "Recount done", what="Batch 3", action="closed")
    assert text == "Batch 3 closed although it doesn't balance (off by 2.000 kg). Reason: Recount done"


@pytest.mark.asyncio
async def test_an_override_with_nothing_to_check_says_so():
    """No saved accounting and no figures from the screen: the override closes
    it, and the remark must not claim it was 0.000 kg off — nothing was checked."""
    refusal, override = await _check(_BatchConn(None), allow_unbalanced=True)
    assert refusal is None
    assert override == {"difference_kg": None, "allowed_kg": None}
    text = bo.override_remark(override["difference_kg"], override["allowed_kg"], "Urgent dispatch",
                              what="Batch 3", action="closed")
    assert text == "Batch 3 closed without a saved balance to check against. Reason: Urgent dispatch"
