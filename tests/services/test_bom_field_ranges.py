"""Column-width guards on the BOM write paths.

bom_line.loss_pct / offgrade_max_pct, bom_process_route.loss_pct and
bom_header.gst_rate are NUMERIC(5,3): 99.999 fits, 100 is a Postgres "numeric
field overflow" — an unhandled 500 that names no field. These tests pin that
both POST and PATCH refuse such values as a 400 `bad_pct` naming the line /
step / field, write nothing, and still accept the largest legal value.

Run:  PYTHONPATH=. python -m pytest tests/services/test_bom_field_ranges.py
"""
from __future__ import annotations

import pytest

from app.modules.bom.services import bom_write_service as svc
from tests.services.test_bom_router import (  # noqa: F401 - client_with is a fixture
    BASE, WriteConn, _line, _payload, client_with,
)
from tests.services.test_bom_router_update_delete import BOM, UpdateConn, _ulines


def test_pct_max_is_the_numeric_5_3_ceiling():
    assert svc.PCT_MAX == 99.999
    assert svc._pct_problem(99.999) is None
    assert svc._pct_problem(0) is None
    assert svc._pct_problem(None) is None
    assert "between 0 and 99.999" in svc._pct_problem(100)
    assert "between 0 and 99.999" in svc._pct_problem(-0.5)
    assert svc._pct_problem("abc") == "must be a number"


# ── Create ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("body, where", [
    ({"lines": [_line(loss_pct=100)]}, {"line_number": 1, "field": "loss_pct"}),
    ({"lines": [_line(), _line(material_sku_name="B", offgrade_max_pct=100)]},
     {"line_number": 2, "field": "offgrade_max_pct"}),
    ({"lines": [_line(loss_pct=-1)]}, {"line_number": 1, "field": "loss_pct"}),
    ({"route": [{"process_name": "Sort"}, {"process_name": "Pack", "loss_pct": 100}]},
     {"step_number": 2, "field": "loss_pct"}),
    ({"gst_rate": 100}, {"field": "gst_rate"}),
])
def test_create_refuses_percentages_the_column_cannot_hold(client_with, body, where):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(**body))
    assert res.status_code == 400, res.text
    detail = res.json()
    assert detail["error"] == "bad_pct"
    for k, v in where.items():
        assert detail["details"][k] == v, detail
    assert conn.sql_of("INSERT INTO") == []


def test_create_accepts_the_largest_legal_percentages(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        gst_rate=99.999,
        lines=[_line(loss_pct=99.999, offgrade_max_pct=99.999)],
        route=[{"process_name": "Pack", "loss_pct": 99.999}],
    ))
    assert res.status_code == 201, res.text
    assert len(conn.sql_of("INSERT INTO bom_line")) == 1
    assert len(conn.sql_of("INSERT INTO bom_process_route")) == 1


# ── Update ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("body, where", [
    ({"lines": _ulines(9001, _line(material_sku_name="B", loss_pct=100))},
     {"line_number": 2, "field": "loss_pct"}),
    ({"lines": [_line(offgrade_max_pct=100.5)]}, {"line_number": 1, "field": "offgrade_max_pct"}),
    ({"route": [{"process_name": "Pack", "loss_pct": 100}]}, {"step_number": 1, "field": "loss_pct"}),
    ({"gst_rate": 100}, {"field": "gst_rate"}),
    ({"notes": "x", "gst_rate": -1}, {"field": "gst_rate"}),
])
def test_update_refuses_percentages_the_column_cannot_hold(client_with, body, where):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json=body)
    assert res.status_code == 400, res.text
    detail = res.json()
    assert detail["error"] == "bad_pct"
    for k, v in where.items():
        assert detail["details"][k] == v, detail
    assert conn.writes() == [], "nothing may be written when one value is refused"
    assert conn.kinds()[-1] == "ROLLBACK"


def test_update_accepts_the_largest_legal_percentages(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={
        "gst_rate": 99.999,
        "lines": _ulines(_line(bom_line_id=9001, loss_pct=99.999, offgrade_max_pct=99.999)),
        "route": [{"process_name": "Pack", "loss_pct": 99.999}],
    })
    assert res.status_code == 200, res.text
    assert len(conn.sql_of("UPDATE bom_line SET line_number = $1")) == 1
    assert len(conn.sql_of("INSERT INTO bom_process_route")) == 1
