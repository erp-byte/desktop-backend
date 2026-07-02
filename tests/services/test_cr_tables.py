"""Pure-logic test for the company->table whitelist. Run:
    PYTHONPATH=. python tests/services/test_cr_tables.py
"""
from fastapi import HTTPException
from app.modules.customer_returns.tables import cr_table_names


def main() -> None:
    assert cr_table_names("CFPL") == {
        "header": "cfpl_customer_return_header",
        "lines": "cfpl_customer_return_lines",
        "boxes": "cfpl_customer_return_boxes",
    }
    assert cr_table_names("cdpl")["header"] == "cdpl_customer_return_header"  # case-insensitive
    for bad in ("", None, "XYZ", "cfpl; DROP TABLE"):
        try:
            cr_table_names(bad)
            raise AssertionError(f"expected 400 for {bad!r}")
        except HTTPException as e:
            assert e.status_code == 400 and e.detail["error"] == "invalid_company"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
