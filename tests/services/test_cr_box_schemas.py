"""Pure-logic tests for the Phase-2 box schemas. Run:
    PYTHONPATH=. python tests/services/test_cr_box_schemas.py
"""
from decimal import Decimal
from pydantic import ValidationError
from app.modules.customer_returns import schemas


def main() -> None:
    # box_number must be >= 1
    try:
        schemas.CRBoxUpsertRequest(article_description="ALMOND W-320", box_number=0)
        raise AssertionError("expected box_number ge=1 error")
    except ValidationError:
        pass

    # Decimal18_3 accepts a 3dp weight; optionals default None
    up = schemas.CRBoxUpsertRequest(
        article_description="ALMOND W-320", box_number=1,
        net_weight=Decimal("25.750"), gross_weight=Decimal("26.000"), count=40,
    )
    assert up.net_weight == Decimal("25.750") and up.lot_number is None

    bulk = schemas.CRBulkBoxUpdateRequest(boxes=[
        schemas.CRBulkBoxItem(article_description="A", box_number=1, net_weight=Decimal("1.5")),
    ])
    assert len(bulk.boxes) == 1

    log = schemas.CRBoxEditLogRequest(
        email_id="x@y.in", box_id="50123456-1", rtv_id="CR-20260703120000",
        changes=[schemas.CRBoxEditLogEntry(field_name="net_weight", old_value="25", new_value="24")],
    )
    assert log.changes[0].field_name == "net_weight"

    resp = schemas.CRBulkBoxUpdateResponse(status="synced", rtv_id="CR-1")
    assert resp.inserted == 0 and resp.deleted == 0
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
