"""Unit tests for compute_output_row — the FG-output validation / yield rule.

Regression for the packaging-stage save bug: recording FG output with no RM
consumption (rm_consumed_kg=None) must NOT be rejected. rm_consumed_kg is
optional and normalizes to 0 (job_card_output_v2.rm_consumed_kg is NOT NULL
DEFAULT 0); yield is then left uncomputed. Mirrors the optional-RM handling
already in job_card_engine / job_card_phase_v2.
"""
from __future__ import annotations

from app.modules.production.services.output_calc import compute_output_row


def test_missing_rm_consumed_is_allowed():
    """The bug: rm_consumed_kg=None must normalize to 0, not error."""
    out = compute_output_row(output_qty_kg=1189.0, rm_consumed_kg=None)
    assert "error" not in out
    assert out["rm_consumed_kg"] == 0.0
    assert out["output_qty_kg"] == 1189.0
    assert out["yield_pct"] is None  # no input recorded → no yield


def test_missing_output_qty_errors():
    """output_qty_kg is the one genuinely required field."""
    assert compute_output_row(output_qty_kg=None, rm_consumed_kg=5.0) == {"error": "missing_qty"}


def test_zero_rm_consumed_no_yield():
    """Explicit 0 RM consumption behaves like None — saved, no yield."""
    out = compute_output_row(output_qty_kg=100.0, rm_consumed_kg=0.0)
    assert "error" not in out
    assert out["rm_consumed_kg"] == 0.0
    assert out["yield_pct"] is None


def test_yield_computed_when_rm_positive():
    out = compute_output_row(output_qty_kg=90.0, rm_consumed_kg=100.0)
    assert out["yield_pct"] == 90.0
    assert out["rm_consumed_kg"] == 100.0
    assert out["output_qty_kg"] == 90.0


def test_negative_qty_rejected():
    assert compute_output_row(output_qty_kg=-1.0, rm_consumed_kg=10.0) == {"error": "negative_qty"}
    assert compute_output_row(output_qty_kg=10.0, rm_consumed_kg=-1.0) == {"error": "negative_qty"}


def test_implausible_yield_rejected():
    """Unit-typo guard preserved: >= +/-1000% yield is rejected loudly."""
    out = compute_output_row(output_qty_kg=5000.0, rm_consumed_kg=1.0)  # 500000%
    assert out["error"] == "implausible_yield"
    assert out["rm_consumed_kg"] == 1.0
    assert out["output_qty_kg"] == 5000.0
