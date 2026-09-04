"""Master-item enrichment in POST /api/v1/po/preview.

Covers the contract in purchase/FRONTEND_API_DOC.md ("What gets matched from
all_sku"): every po_line field sourced from all_sku, the line-level `uom`
string vs `matched_item.uom` float split, and the fuzzy cut-off scale.

Regression origin: the preview returned 357/357 ResponseValidationErrors
("uom: Input should be a valid string", input 1.0) because MasterItem.uom is a
float unit-weight, not a unit label.
"""
import pytest

from app.modules.purchase.schemas.po_api import PreviewLine
from app.modules.purchase.services import po_preview as P
from app.modules.so.services.item_matcher import MasterItem, match_sku

CHIA = MasterItem(
    particulars="black chia seeds", item_type="rm", group="seeds",
    sub_group="chia", uom=1.0, sale_group="bulk", gst=0.05,
)
SUGAR = MasterItem(
    particulars="sugar refined", item_type="rm", group="sweetener",
    sub_group="sugar", uom=50.0, sale_group="bulk", gst=0.05,
)


def _line(**kw):
    base = {"line_number": 1, "sku_name": "black chia seeds", "pack_count": 10,
            "uom": None, "rate": 247.0, "amount": 2470.0}
    base.update(kw)
    return base


# ── the reported 500 ──────────────────────────────────────────────────────

def test_line_uom_is_a_string_so_the_response_validates():
    out = P._enrich_line_from_master(_line(), CHIA, 0.95)
    assert isinstance(out["uom"], str), "MasterItem.uom is a float unit-weight"
    assert out["uom"] == "1.0"
    PreviewLine(**out)          # must not raise — this is the reported 500


def test_master_uom_wins_over_the_parsed_alt_units_column():
    """uom is an all_sku-sourced field. Col K (Alt. Units) is Tally's secondary
    quantity, not a unit weight -- e.g. 20000.0 for 'Deri Dates'. Letting it
    stand would put a nonsense multiplier into po_weight."""
    out = P._enrich_line_from_master(_line(uom="20000.0"), CHIA, 0.95)
    assert out["uom"] == "1.0"


def test_po_weight_uses_master_uom_not_the_alt_units_column():
    out = P._enrich_line_from_master(_line(uom="20000.0", pack_count=10), CHIA, 0.95)
    assert out["po_weight"] == 10.0


def test_uom_absent_when_master_has_none():
    no_uom = MasterItem("x", "rm", "g", "sg", None, "bulk", 0.05)
    out = P._enrich_line_from_master(_line(), no_uom, 0.95)
    assert out["uom"] is None
    PreviewLine(**out)


# ── silently-dropped master fields ────────────────────────────────────────

@pytest.mark.parametrize("key,expected", [
    ("particulars",   "black chia seeds"),
    ("item_category", "seeds"),
    ("sub_category",  "chia"),
    ("item_type",     "rm"),
    ("sales_group",   "bulk"),
    ("gst_rate",      0.05),
])
def test_master_fields_reach_the_line(key, expected):
    assert P._enrich_line_from_master(_line(), CHIA, 0.95)[key] == expected


def test_parsed_values_still_win_over_master():
    out = P._enrich_line_from_master(_line(item_category="OVERRIDE"), CHIA, 0.95)
    assert out["item_category"] == "OVERRIDE"


@pytest.mark.parametrize("key,expected", [
    ("sku_name",      "black chia seeds"),
    ("item_category", "seeds"),
    ("sub_category",  "chia"),
    ("item_type",     "rm"),
    ("sales_group",   "bulk"),
    ("gst_rate",      0.05),
    ("uom",           1.0),      # float here — frontend does pack_count x uom
])
def test_matched_item_payload_is_complete(key, expected):
    assert P._matched_item_payload(CHIA)[key] == expected


def test_matched_item_uom_stays_numeric():
    assert isinstance(P._matched_item_payload(CHIA)["uom"], float)


def test_matched_item_none_passes_through():
    assert P._matched_item_payload(None) is None


# ── po_weight ─────────────────────────────────────────────────────────────

def test_po_weight_computed_from_pack_count_and_master_uom():
    assert P._enrich_line_from_master(_line(pack_count=10000), CHIA, 0.95)["po_weight"] == 10000.0


def test_po_weight_none_without_pack_count():
    """No pack_count -> nothing to multiply. The key is simply not invented,
    which is how every other gap in this function behaves; the response model
    supplies the null."""
    out = P._enrich_line_from_master(_line(pack_count=None), CHIA, 0.95)
    assert out.get("po_weight") is None
    assert PreviewLine(**out).po_weight is None


def test_po_weight_not_overwritten_when_already_present():
    assert P._enrich_line_from_master(_line(po_weight=7.5), CHIA, 0.95)["po_weight"] == 7.5


# ── fuzzy cut-off scale ───────────────────────────────────────────────────

def test_threshold_is_passed_on_the_0_to_100_scale():
    """_FUZZY_THRESHOLD is 0-1 for classification; match_sku wants 0-100.
    Passing 0.70 straight through disables the cut-off entirely."""
    junk = "ZZZZ TOTALLY UNRELATED WIDGET"
    item, score = match_sku(junk, [CHIA, SUGAR], P._FUZZY_THRESHOLD * 100)
    assert item is None and score == 0.0


def test_unmatched_line_gets_no_master_data():
    out = P._enrich_line_from_master(_line(sku_name="ZZZZ UNRELATED", uom=None), None, 0.0)
    assert out["match_source"] == "none"
    for k in ("uom", "item_type", "item_category", "gst_rate", "po_weight"):
        assert out.get(k) is None, f"{k} grafted from an unmatched item"
    PreviewLine(**out)
