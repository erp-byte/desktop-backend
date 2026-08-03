"""The Bulk / VA / RPC classifier that decides whether a general sample needs production.

Getting this wrong is expensive in both directions: classifying VA as Bulk skips a
production run the sample needed, and classifying Bulk as VA raises a job card for stock
that was ready to issue. The cases below are the real sale_group values in the live master
(4708 rows), not invented ones.
"""
import pytest

from app.modules.sample.services import sample_form as sf


@pytest.mark.parametrize("sale_group,expected", [
    # exactly as stored in all_sku
    ("va", sf.VA),
    ("rpc", sf.RPC),
    ("bulk", sf.BULK),
    # `dates` is a product family, not a form — it must not change the routing
    ("dates rpc", sf.RPC),
    ("dates bulk", sf.BULK),
    ("dates ing", sf.ING),
    # tolerate whatever casing/spacing the master picks up
    ("VA", sf.VA),
    ("  Dates   RPC ", sf.RPC),
    ("Bulk", sf.BULK),
])
def test_known_sale_groups_classify(sale_group, expected):
    assert sf.classify(sale_group) == expected


@pytest.mark.parametrize("sale_group", [
    None, "", "   ",
    "pm",      # packaging material — not a sampled product form
    "wip",     # work in progress
    "0",       # one junk row in the master
    "dates",   # family prefix with no form after it
    "something new",
])
def test_unclassifiable_returns_none(sale_group):
    """None is a real answer. 105 FGs carry no sale_group at all, and the module must ask
    rather than pick a route for them."""
    assert sf.classify(sale_group) is None


def test_va_and_rpc_need_a_job_card_bulk_does_not():
    assert sf.needs_job_card(sf.VA) is True
    assert sf.needs_job_card(sf.RPC) is True
    assert sf.needs_job_card(sf.BULK) is False


def test_ingredient_is_issued_directly():
    """An ingredient is sampled as-is — there is nothing to add value to or repack."""
    assert sf.needs_job_card(sf.ING) is False


def test_unknown_form_never_triggers_production():
    """The dangerous direction: an unrecognised article must not raise a job card by
    default. Callers block on classify() returning None; this is the second line."""
    for bad in (None, "", "VA ", "va", "anything"):
        assert sf.needs_job_card(bad) is False


def test_every_live_sale_group_is_accounted_for():
    """Guards against a new value appearing in the master and silently becoming unroutable.
    If this fails, decide the routing for the new value — do not just add it here."""
    live_fg_values = {"va", "rpc", "dates rpc", "bulk", "dates ing", "dates bulk"}
    unclassified = {v for v in live_fg_values if sf.classify(v) is None}
    assert not unclassified, f"live FG sale_groups with no form: {unclassified}"


def test_labels_exist_for_every_form():
    for form in (sf.BULK, sf.VA, sf.RPC, sf.ING):
        assert sf.label(form) and sf.label(form) != "Unclassified"
    assert sf.label(None) == "Unclassified"
