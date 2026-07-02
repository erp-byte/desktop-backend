"""Pure-logic tests for query_service helpers/mappers. Run:
    PYTHONPATH=. python tests/services/test_cr_helpers.py
"""
from datetime import date
from fastapi import HTTPException
from app.modules.customer_returns.services import query_service as q


def main() -> None:
    assert q._to_float("3.5") == 3.5
    assert q._to_float(None) is None and q._to_float("x") is None

    # value = qty*rate unless a positive value is supplied.
    assert q._line_value(4, 10.0, "0") == 40.0
    assert q._line_value(4, 10.0, "") == 40.0
    assert q._line_value(4, 10.0, "55") == 55.0

    assert q._convert_date("09-06-2026") == date(2026, 6, 9)
    assert q._convert_date(None) is None
    try:
        q._convert_date("2026/06/09")
        raise AssertionError("expected 400 on bad date")
    except HTTPException as e:
        assert e.status_code == 400

    # numeric->string with '0' default; mapper produces string numerics.
    assert q._num_str(None) == "0" and q._num_str(12) == "12"
    row = {"rtv_id": "CR-1", "item_description": "A", "material_type": "RM",
           "item_category": "N", "sub_category": "S", "uom": "KG",
           "qty": 4, "rate": 10, "value": 40, "net_weight": 25, "carton_weight": 0,
           "lot_number": None, "item_mark": None, "spl_remarks": None, "vakkal": None,
           "created_at": None, "updated_at": None}
    m = q._map_line_row(row)
    assert m["qty"] == "4" and m["value"] == "40" and m["rtv_id"] == "CR-1"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
