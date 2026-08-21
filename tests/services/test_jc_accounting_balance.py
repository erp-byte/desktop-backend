"""The R9 conservation identity must not count a stage's production twice.

Field report (20 Aug 2026): JC 74337017 "Sorting" (PLAN-73302918-L73302927-S1)
refused to close with "Accounting is unbalanced" while the Output & Accounting
tab showed a green "Balanced / 0.00 kg". The persisted row said otherwise:

    total_input_qty     150.0
    output_qty          149.8
    dispatched_out_qty  149.8      <-- the SAME 149.8 kg, counted again
    offgrade_total_qty    0.2
    total_accounted_qty 299.8      = 149.8 + 149.8 + 0.2
    balance_difference -149.8      = exactly -dispatched_out_qty
    is_balanced         false

The module docstring claims `output_qty` and `dispatched_out_qty` PARTITION
production ("never overlap"), which is what licensed summing both. No writer
honours that: `output_qty` is the batch's full `fg_actual_kg` / `produced_qty_kg`,
and `job_card_v2.dispatched_to_next_kg` — which `dispatched_out_qty` mirrors —
is incremented by close_batch's auto-dispatch with that same full produced qty
(job_card_batch_v2:448 `if dispatch_qty_kg is None: effective_dispatch =
produced_qty_kg`). So dispatched_out is always a SUBSET of output_qty, and every
dispatching stage is structurally unbalanceable.

`dispatched_out_qty` is therefore an audit mirror, exactly like `carried_in_qty`
on the input side (which the equation already excludes because `total_input_qty`
is comprehensive). Symmetric rule: `output_qty` is comprehensive, so the OUT
side excludes the dispatch mirror.

Guards (no DB — a fake conn):
  1. the JC 74337017 repro balances;
  2. a partial dispatch doesn't shift the balance either;
  3. a zero-dispatch JC is unaffected (no regression on terminal stages);
  4. a GENUINE variance is still refused — the gate isn't merely disabled;
  5. return_qty still counts on the OUT side (RM back to stores is real mass);
  6. the mirror is still persisted for audit.

Run:  PYTHONPATH=. python -m pytest tests/services/test_jc_accounting_balance.py
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.production.services import jc_accounting_v2 as acct_svc

JC = 74337017
BATCH = 22384101


class _Txn:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Conn:
    """Answers the handful of reads save_accounting issues; captures the upsert."""

    def __init__(self, *, dispatched_to_next_kg=0.0, carried_qty_kg=0.0,
                 total_return=0.0, tolerance_pct=Decimal("0.0001")):
        self.dispatched_to_next_kg = dispatched_to_next_kg
        self.carried_qty_kg = carried_qty_kg
        self.total_return = total_return
        self.tolerance_pct = tolerance_pct
        self.inserted: dict | None = None

    def transaction(self):
        return _Txn()

    def is_in_transaction(self):
        # insert_with_pk_retry refuses to open a SAVEPOINT outside an outer txn.
        return True

    async def execute(self, sql, *p):
        return "OK"

    async def fetchval(self, sql, *p):
        flat = " ".join(sql.split())
        if "SUM(return_qty)" in flat:
            return Decimal(str(self.total_return))
        if "allowed_balance_tolerance_pct" in flat:
            return self.tolerance_pct
        if "information_schema.columns" in flat:
            return True          # post-049 schema
        raise AssertionError(f"unstubbed fetchval: {flat}")

    async def fetchrow(self, sql, *p):
        flat = " ".join(sql.split())
        if "SELECT is_locked" in flat:
            return {"is_locked": False, "locked_reason": None,
                    "force_unlocked": False, "status": "in_progress"}
        if "SELECT job_card_id, output_kind, uom" in flat:
            return {"job_card_id": JC, "output_kind": "SFG", "uom": "KGS",
                    "carried_qty_kg": Decimal(str(self.carried_qty_kg)),
                    "dispatched_to_next_kg": Decimal(str(self.dispatched_to_next_kg)),
                    "bom_id": 590}
        if flat.startswith("SELECT total_input_qty"):
            return None          # first save — no prior row, no edit-log noise
        if flat.startswith("INSERT INTO job_card_accounting_v2"):
            # Positional args mirror the VALUES list of the post-049 upsert.
            self.inserted = {
                "accounting_id": p[0], "job_card_id": p[1], "batch_id": p[2],
                "total_input_qty": p[3], "output_qty": p[5],
                "carried_in_qty": p[9], "dispatched_out_qty": p[10],
                "total_accounted_qty": p[19], "balance_difference_qty": p[20],
                "is_balanced": p[21],
            }
            return dict(self.inserted)
        raise AssertionError(f"unstubbed fetchrow: {flat}")


@pytest.fixture(autouse=True)
def _no_variance_capture(monkeypatch):
    """B12 variance capture is a separate concern and needs its own tables."""
    async def _noop(conn, job_card_id):
        return {"rows": 0, "skipped": []}
    monkeypatch.setattr(acct_svc, "_record_consumption_variance", _noop)


async def _save(conn, **payload):
    base = {"total_input_qty": 0.0, "output_qty": 0.0, "process_loss_qty": 0.0,
            "extra_give_away_qty": 0.0, "balance_material_qty": 0.0,
            "offgrade_total_qty": 0.0, "rejection_qty": 0.0, "wastage_qty": 0.0,
            "control_sample_qty": 0.0, "input_uom": "KGS", "output_uom": "KGS"}
    base.update(payload)
    return await acct_svc.save_accounting(
        conn, job_card_id=JC, payload=base, saved_by="test", batch_id=BATCH,
    )


@pytest.mark.asyncio
async def test_dispatched_output_is_not_counted_twice():
    """JC 74337017 verbatim: 150 in, 149.8 produced + fully dispatched, 0.2 off-grade."""
    conn = _Conn(dispatched_to_next_kg=149.8)
    res = await _save(conn, total_input_qty=150.0, output_qty=149.8,
                      offgrade_total_qty=0.2)

    assert res["total_accounted_qty"] == pytest.approx(150.0), (
        "OUT side must be output + off-grade, not output + dispatch + off-grade"
    )
    assert res["balance_difference_qty"] == pytest.approx(0.0)
    assert res["is_balanced"] is True


@pytest.mark.asyncio
async def test_partial_dispatch_does_not_shift_the_balance():
    """How much of the output has moved downstream is irrelevant to conservation."""
    conn = _Conn(dispatched_to_next_kg=100.0)
    res = await _save(conn, total_input_qty=150.0, output_qty=149.8,
                      offgrade_total_qty=0.2)

    assert res["balance_difference_qty"] == pytest.approx(0.0)
    assert res["is_balanced"] is True


@pytest.mark.asyncio
async def test_terminal_stage_with_no_dispatch_unaffected():
    """Regression guard: stages that never dispatch behaved correctly already."""
    conn = _Conn(dispatched_to_next_kg=0.0)
    res = await _save(conn, total_input_qty=150.0, output_qty=149.8,
                      offgrade_total_qty=0.2)

    assert res["balance_difference_qty"] == pytest.approx(0.0)
    assert res["is_balanced"] is True


@pytest.mark.asyncio
async def test_genuine_variance_is_still_refused():
    """The gate must still bite: 10 kg of unexplained mass stays unbalanced."""
    conn = _Conn(dispatched_to_next_kg=139.8)
    res = await _save(conn, total_input_qty=150.0, output_qty=139.8,
                      offgrade_total_qty=0.2)

    assert res["balance_difference_qty"] == pytest.approx(10.0)
    assert res["is_balanced"] is False


@pytest.mark.asyncio
async def test_returns_still_count_on_the_out_side():
    """RM sent back to stores is real mass leaving the stage — unlike a dispatch,
    it is NOT already inside output_qty."""
    conn = _Conn(dispatched_to_next_kg=139.8, total_return=10.0)
    res = await _save(conn, total_input_qty=150.0, output_qty=139.8,
                      offgrade_total_qty=0.2)

    assert res["total_accounted_qty"] == pytest.approx(150.0)
    assert res["is_balanced"] is True


@pytest.mark.asyncio
async def test_dispatch_mirror_is_still_persisted_for_audit():
    """Dropping it from the equation must not drop it from the row."""
    conn = _Conn(dispatched_to_next_kg=149.8, carried_qty_kg=12.0)
    await _save(conn, total_input_qty=150.0, output_qty=149.8,
                offgrade_total_qty=0.2)

    assert conn.inserted is not None
    assert conn.inserted["dispatched_out_qty"] == pytest.approx(149.8)
    assert conn.inserted["carried_in_qty"] == pytest.approx(12.0)
    assert conn.inserted["total_accounted_qty"] == pytest.approx(150.0)
