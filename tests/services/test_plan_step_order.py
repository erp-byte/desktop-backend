"""Unit tests for stage-order normalization in plan_v2.

Regression for the inverted stage-order bug: a plan line whose route was
snapshotted as {Packaging, Sorting} (observed on "Solimo Premium Pine Nuts,
250g" in plan 58795071) makes the chain lock Sorting *behind* Packaging — so
the floor is told to pack before it sorts. create_plan must normalize
packing-class stages to the END of the route while preserving the relative
order of every other stage.
"""
from app.modules.production.services.plan_v2 import order_steps_packing_last


def _names(steps):
    return [s["process_name"] for s in steps]


def test_packaging_before_sorting_is_reordered():
    """The bug: {Packaging, Sorting} must become {Sorting, Packaging}."""
    steps = [
        {"process_name": "Packaging", "stage": "packaging"},
        {"process_name": "Sorting", "stage": "sorting"},
    ]
    assert _names(order_steps_packing_last(steps)) == ["Sorting", "Packaging"]


def test_correct_order_is_preserved():
    steps = [
        {"process_name": "Sorting", "stage": "sorting"},
        {"process_name": "Roasting", "stage": "roasting"},
        {"process_name": "Packaging", "stage": "packaging"},
    ]
    assert _names(order_steps_packing_last(steps)) == ["Sorting", "Roasting", "Packaging"]


def test_relative_order_of_non_packing_preserved():
    """Only packing moves; Flavouring/Sorting keep their given order."""
    steps = [
        {"process_name": "Flavouring", "stage": "flavouring"},
        {"process_name": "Sorting", "stage": "sorting"},
        {"process_name": "Packaging", "stage": "packaging"},
    ]
    assert _names(order_steps_packing_last(steps)) == ["Flavouring", "Sorting", "Packaging"]


def test_single_packaging_only():
    steps = [{"process_name": "Packaging", "stage": "packaging"}]
    assert _names(order_steps_packing_last(steps)) == ["Packaging"]


def test_stage_none_falls_back_to_process_name():
    """Custom steps may carry stage=None; the packing check must still fire
    off process_name (matches create_plan's own stage-derive fallback)."""
    steps = [
        {"process_name": "Packing", "stage": None},
        {"process_name": "Sorting", "stage": None},
    ]
    assert _names(order_steps_packing_last(steps)) == ["Sorting", "Packing"]


def test_multiple_non_packing_then_packing():
    steps = [
        {"process_name": "Packaging", "stage": "packaging"},
        {"process_name": "Cleaning", "stage": "cleaning"},
        {"process_name": "Sorting", "stage": "sorting"},
    ]
    assert _names(order_steps_packing_last(steps)) == ["Cleaning", "Sorting", "Packaging"]


def test_custom_stage_of_callable():
    """A caller may supply its own stage extractor (used for asyncpg Records
    in create_plan, which are subscriptable but not dict.get-able)."""
    steps = [
        {"p": "Packaging", "s": "packaging"},
        {"p": "Sorting", "s": "sorting"},
    ]
    out = order_steps_packing_last(steps, stage_of=lambda s: s["s"])
    assert [s["p"] for s in out] == ["Sorting", "Packaging"]


def test_empty_list():
    assert order_steps_packing_last([]) == []
