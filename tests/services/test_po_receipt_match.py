"""When a PO line, and a PO, counts as received in full (services/receipt_match).

The status the Material In list shows and the receipt summary's ✓ per line come
from these two functions, so they can't disagree.
"""
import pytest

from app.modules.purchase.services.receipt_match import line_match, receiving_status


def line(po_weight=None, pack_count=None, received_weight=0, counted=None, boxes=0, n=1):
    return line_match({
        "line_number": n, "po_weight": po_weight, "pack_count": pack_count,
        "received_weight": received_weight, "counted": counted, "received_boxes": boxes,
    })


# ── one line ─────────────────────────────────────────────────────────────────

def test_raw_material_matches_on_weight_not_on_box_count():
    """Cashew 7000 kg (Tally's quantity is kg) in 280 boxes of 25 kg: matched,
    though 280 boxes is far short of '7000'."""
    l = line(po_weight=7000, pack_count=7000, received_weight=7000, boxes=280)
    assert l["weight_matched"] and l["count_matched"] and l["matched"]


@pytest.mark.parametrize("received,matched", [(6930, True), (6929, False), (7100, True)])
def test_weight_needs_99_percent(received, matched):
    assert line(po_weight=7000, pack_count=7000, received_weight=received, boxes=1)["matched"] is matched


def test_counts_on_the_boxes_are_checked():
    """Pouches: 100000 pcs ordered (200 kg). The boxes record pieces."""
    short = line(po_weight=200, pack_count=100000, received_weight=200, counted=90000, boxes=90)
    assert short["weight_matched"] and not short["count_matched"] and not short["matched"]
    full = line(po_weight=200, pack_count=100000, received_weight=200, counted=100000, boxes=100)
    assert full["matched"]


def test_no_ordered_weight_falls_back_to_the_box_count():
    assert not line(po_weight=None, pack_count=10, boxes=8)["matched"]
    assert line(po_weight=None, pack_count=10, boxes=10)["matched"]


def test_nothing_received_is_not_matched():
    assert not line(po_weight=50, pack_count=50)["matched"]


# ── the PO ───────────────────────────────────────────────────────────────────

def test_no_box_is_pending():
    assert receiving_status([line(po_weight=50, pack_count=50)]) == "pending"


def test_every_line_matched_is_fully_received():
    assert receiving_status([
        line(po_weight=50, pack_count=50, received_weight=50, boxes=2, n=1),
        line(po_weight=10, pack_count=10, received_weight=9.95, boxes=1, n=2),
    ]) == "fully received"


def test_one_line_short_is_partially_received():
    assert receiving_status([
        line(po_weight=50, pack_count=50, received_weight=50, boxes=2, n=1),
        line(po_weight=10, pack_count=10, received_weight=0, boxes=0, n=2),
    ]) == "partially received"


def test_more_than_ordered_is_extra_received():
    assert receiving_status([line(po_weight=50, pack_count=50, received_weight=60, boxes=3)]) == "extra received"


def test_no_ordered_weight_is_never_extra():
    assert receiving_status([line(po_weight=None, pack_count=10, received_weight=30, boxes=10)]) == "fully received"


def test_the_real_rds_po_01906_is_fully_received():
    """CF/PO/2026-27/01906 on RDS: 1 kg ordered, 1 kg in 1 box; stored as
    'partially received' before this rule."""
    assert receiving_status([line(po_weight=1, pack_count=1, received_weight=1, counted=1, boxes=1)]) == "fully received"
