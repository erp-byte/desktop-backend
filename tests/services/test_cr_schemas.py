"""Pure-logic tests for customer-returns schemas. Run:
    PYTHONPATH=. python tests/services/test_cr_schemas.py
"""
from pydantic import ValidationError
from app.modules.customer_returns import schemas


def main() -> None:
    # material_type/uom auto-uppercase; numeric string defaults present.
    line = schemas.CRLineCreate(
        material_type="rm", item_category="Nuts", sub_category="Almond",
        item_description="ALMOND W-320", uom="kg",
    )
    assert line.material_type == "RM" and line.uom == "KG"
    assert line.qty == "0" and line.value == "0" and line.net_weight == "0"

    # CRCreate requires >=1 line.
    try:
        schemas.CRCreate(company="CFPL",
                         header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                         lines=[])
        raise AssertionError("expected min_length validation error")
    except ValidationError:
        pass

    # Company literal rejects junk.
    try:
        schemas.CRCreate(company="XYZ",
                         header=schemas.CRHeaderCreate(factory_unit="A-185", customer="ACME"),
                         lines=[line])
        raise AssertionError("expected company literal error")
    except ValidationError:
        pass

    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
