"""Per-batch totals on a job card's list of WIP boxes.

``get_boxes_for_jc`` answers the job card's Boxes tab: every box the card has
made, and — per accounting batch — how many of them are boxed, how many are
printed and how much they weigh. The totals are counted in Python from the one
box query, so here a scripted connection answers that query and the tests read
what comes back. Only the card's SFG boxes are read, so a box printed on the
Raw Material tab (item_type 'rm') can never be counted.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.modules.production.services import sfg_box_service

JC = 75009889
B1, B2 = 41000001, 41000002


class FakeConn:
    """Answers the box query with scripted rows and logs every call in order."""

    def __init__(self, rows=()):
        self.rows = [dict(r) for r in rows]
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append((s, args))
        if s.startswith("SELECT carton_id AS box_id"):
            return self.rows
        raise AssertionError(f"unexpected fetch: {s[:90]}")


def _box(batch_id=B1, net=10.0, status="PRINTED", box_id="48213307-1"):
    return {"box_id": box_id, "item_type": "sfg", "job_card_id": JC, "batch_id": batch_id,
            "batch_code": "L-7", "net_weight": net, "gross_weight": None, "units": None,
            "status": status}


def listed(rows):
    conn = FakeConn(rows)
    return asyncio.run(sfg_box_service.get_boxes_for_jc(conn, JC)), conn


def test_each_batch_gets_its_own_box_count_printed_count_and_net_weight():
    out, _ = listed([
        _box(B2, net=8.0, status="PENDING", box_id="48213307-1"),
        _box(B1, net=10.0, box_id="48213307-2"),
        _box(B1, net=9.5, status="PENDING", box_id="48213307-3"),
        _box(B2, net=7.25, box_id="48213307-4"),
    ])
    assert out["by_batch"] == [
        {"batch_id": B1, "boxes": 2, "printed": 1, "net_kg": 19.5},
        {"batch_id": B2, "boxes": 2, "printed": 1, "net_kg": 15.25},
    ]
    assert out["printed_count"] == 2


def test_batches_are_listed_by_batch_id():
    out, _ = listed([_box(B2, box_id="48213307-1"), _box(B1, box_id="48213307-2")])
    assert [e["batch_id"] for e in out["by_batch"]] == [B1, B2]


def test_boxes_with_no_batch_are_one_entry_and_come_last():
    out, _ = listed([
        _box(None, net=4.0, box_id="48213307-1"),
        _box(B2, net=8.0, box_id="48213307-2"),
        _box(None, net=1.5, status="PENDING", box_id="48213307-3"),
        _box(B1, net=10.0, box_id="48213307-4"),
    ])
    assert out["by_batch"] == [
        {"batch_id": B1, "boxes": 1, "printed": 1, "net_kg": 10.0},
        {"batch_id": B2, "boxes": 1, "printed": 1, "net_kg": 8.0},
        {"batch_id": None, "boxes": 2, "printed": 1, "net_kg": 5.5},
    ]


def test_no_entry_without_a_box_to_put_in_it():
    out, _ = listed([_box(B1)])
    assert [e["batch_id"] for e in out["by_batch"]] == [B1]


@pytest.mark.parametrize("status", ["PRINTED", "DISPATCHED", "RECEIVED", "CONSUMED"])
def test_a_box_past_pending_still_counts_as_printed(status):
    # Only a PENDING box is still waiting for its label: a box reaches DISPATCHED
    # / RECEIVED / CONSUMED only through PRINTED, and the next stage scanning it
    # in flips THIS card's row to RECEIVED. Counting only PRINTED would un-print
    # a box hours after its label came off the printer, and the boxes tab would
    # tell the operator to print labels that already exist.
    out, _ = listed([_box(B1, status=status, box_id="48213307-1"),
                     _box(B1, status="PENDING", box_id="48213307-2")])
    assert out["printed_count"] == 1
    assert out["by_batch"] == [{"batch_id": B1, "boxes": 2, "printed": 1, "net_kg": 20.0}]


def test_a_batch_with_nothing_printed_yet_says_so():
    out, _ = listed([_box(B1, status="PENDING", box_id="48213307-1")])
    assert out["printed_count"] == 0 and out["by_batch"][0]["printed"] == 0


def test_a_cancelled_box_is_listed_but_left_out_of_the_rollup():
    # A cancelled box is neither boxed material nor a label waiting to be printed,
    # so it counts in neither boxes, printed nor net_kg. count / total_net_kg are
    # the pre-existing totals and keep listing it.
    out, _ = listed([_box(B1, net=10.0, box_id="48213307-1"),
                     _box(B1, net=7.0, status="CANCELLED", box_id="48213307-2")])
    assert out["by_batch"] == [{"batch_id": B1, "boxes": 1, "printed": 1, "net_kg": 10.0}]
    assert out["printed_count"] == 1
    assert (out["count"], out["total_net_kg"]) == (2, 17.0)
    assert [b["box_id"] for b in out["boxes"]] == ["48213307-1", "48213307-2"]


def test_a_batch_whose_only_box_is_cancelled_gets_no_entry():
    out, _ = listed([_box(B1, status="CANCELLED", box_id="48213307-1"),
                     _box(B2, box_id="48213307-2")])
    assert [e["batch_id"] for e in out["by_batch"]] == [B2]


def test_net_weight_is_rounded_to_three_decimals():
    # Weights come back as numerics and add up with a float tail (0.1 + 0.2 + 0.3);
    # both the batch total and the job card's total are rounded, not left raw.
    out, _ = listed([
        _box(B1, net=Decimal("0.1"), box_id="48213307-1"),
        _box(B1, net=Decimal("0.2"), box_id="48213307-2"),
        _box(B2, net=Decimal("0.3"), box_id="48213307-3"),
        _box(B2, net=Decimal("9.7774"), box_id="48213307-4"),
    ])
    assert [e["net_kg"] for e in out["by_batch"]] == [0.3, 10.077]
    assert out["total_net_kg"] == 10.377


def test_a_job_card_with_no_boxes_totals_nothing():
    out, _ = listed([])
    assert out == {"job_card_id": JC, "count": 0, "total_net_kg": 0.0,
                   "printed_count": 0, "by_batch": [], "boxes": []}


def test_the_boxes_and_their_totals_still_come_back_whole():
    rows = [_box(B1, net=10.0, box_id="48213307-1"), _box(B2, net=8.0, box_id="48213307-2")]
    out, _ = listed(rows)
    assert (out["job_card_id"], out["count"], out["total_net_kg"]) == (JC, 2, 18.0)
    assert out["boxes"] == rows


def test_only_this_job_cards_sfg_boxes_are_read():
    # One query, and it leaves out the RM boxes printed on the Raw Material tab —
    # otherwise a store print would show up as a produced box on this job card.
    out, conn = listed([_box(B1)])
    [(sql, args)] = conn.calls
    assert "FROM sfg_box" in sql and "WHERE job_card_id = $1 AND item_type = 'sfg'" in sql
    assert args == (JC,)
