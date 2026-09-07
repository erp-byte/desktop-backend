"""The queue's six display statuses, computed and filtered server-side.

The queue and the dashboard both read list_requisitions, so the collapse from 13 raw
lifecycle states to the six a human recognises happens ONCE, in SQL:

    Pending      DRAFT, SUBMITTED
    Hold         ON_HOLD                    (the reason rides along for the tooltip)
    In process   accepted, nothing shipped
    Partial      something shipped, less than asked for
    Dispatched   all of it shipped, or the gate pass closed it
    Cancelled    CANCELLED, BH_REJECTED

It is SQL rather than Python because the filter has to be a WHERE: filtering in Python
after the fetch would page against the unfiltered row set and report the wrong totals.

Two facts shape the rule and are pinned below:
  * only NPD/TRIAL raise a dev job card, so most sample types have no dispatch ledger at
    all — the lifecycle status alone must be able to reach Dispatched.
  * the ledger's uom varies (kg, gm, pcs) while sample_requisitions.quantity is kg, so a
    mixed-unit sum is never allowed to claim "complete".

Run:  PYTHONPATH=. python -m pytest tests/services/test_requisition_display_status.py
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.sample.services import requisition_service as rs


class _Conn:
    """Records the SQL and args; answers the migration probe and the nested reads."""

    def __init__(self, *, cols=("dispatch_id", "dev_jc_id", "qty", "uom", "article_id")):
        self.cols = list(cols)
        self.queries: list[str] = []
        self.args: list[tuple] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        self.args.append(args)
        if "information_schema.columns" in query:
            return [{"column_name": c} for c in self.cols]
        return []


def _list(**kw):
    conn = _Conn(**kw.pop("conn_kw", {}))
    asyncio.run(rs.list_requisitions(conn, **kw))
    main = next(q for q in conn.queries if "FROM sample_requisitions" in q)
    return conn, main


# --- the computed column ----------------------------------------------------

def test_the_list_computes_a_display_status():
    _, sql = _list()
    assert "AS display_status" in sql


def test_every_bucket_is_reachable():
    _, sql = _list()
    for bucket in ("HOLD", "CANCELLED", "PENDING", "DISPATCHED", "PARTIAL", "IN_PROCESS"):
        assert f"'{bucket}'" in sql, bucket


def test_hold_is_resolved_before_anything_else():
    """A held request that already part-shipped is still on hold — that is the state the
    reviewer has to act on, and Partial would hide it."""
    _, sql = _list()
    assert sql.index("'HOLD'") < sql.index("'PARTIAL'")


def test_cancelled_outranks_the_quantity_check():
    """A cancelled request whose stock had already gone must not read as Dispatched."""
    _, sql = _list()
    assert sql.index("'CANCELLED'") < sql.index("'PARTIAL'")


def test_the_gate_pass_statuses_reach_dispatched_without_the_ledger():
    """Only NPD and TRIAL raise a dev job card. Without this a BASIS_RM sample would sit
    at In process forever, because it can never have a dispatch row."""
    _, sql = _list()
    assert "GATE_PASS_ISSUED" in sql and "CLOSED" in sql


def test_a_mixed_unit_ledger_can_never_claim_complete():
    """quantity is kg; the ledger's uom may be gm or pcs. Summing across units and
    comparing would silently mark a part-shipped request Dispatched."""
    _, sql = _list()
    assert "uom_kinds" in sql


# --- the filter -------------------------------------------------------------

def test_the_filter_is_a_where_clause_not_a_python_pass():
    """Filtering after the fetch would page against the unfiltered set: page 2 of
    'Partial' would skip rows that were never shown on page 1."""
    _, sql = _list(display_statuses=["PARTIAL"])
    assert "display_status = ANY" in sql


def test_the_filter_value_is_passed_as_a_parameter():
    conn, _ = _list(display_statuses=["PARTIAL", "DISPATCHED"])
    args = next(a for q, a in zip(conn.queries, conn.args) if "FROM sample_requisitions" in q)
    assert ["PARTIAL", "DISPATCHED"] in args


def test_no_filter_returns_everything():
    conn, _ = _list()
    args = next(a for q, a in zip(conn.queries, conn.args) if "FROM sample_requisitions" in q)
    assert None in args


def test_the_filter_composes_with_the_existing_status_filter():
    """The raw-status filter still exists for the NPD queue's buckets; the two must be
    separate predicates, not one overwriting the other."""
    _, sql = _list(statuses=["ON_HOLD"], display_statuses=["HOLD"])
    assert "status = ANY" in sql and "display_status = ANY" in sql


# --- hand-applied migrations ------------------------------------------------

def test_an_unmigrated_ledger_still_lists():
    """npd_dev_dispatch is 078 and its uom column is 084, both hand-applied. A missing
    table must degrade to status-only buckets, not 500 the whole queue."""
    _, sql = _list(conn_kw={"cols": ()})
    assert "AS display_status" in sql
    assert "npd_dev_dispatch" not in sql


def test_a_ledger_without_the_uom_column_falls_back_to_the_card():
    """084 added uom nullable with no backfill; before it, the card's own unit is the
    only answer available."""
    _, sql = _list(conn_kw={"cols": ("dispatch_id", "dev_jc_id", "qty")})
    assert "npd_dev_dispatch" in sql
    assert "d.uom" not in sql


def test_the_probe_runs_once_per_list_call():
    conn, _ = _list()
    assert sum("information_schema.columns" in q for q in conn.queries) == 1
