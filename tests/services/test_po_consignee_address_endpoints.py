"""consignee_address across the purchase endpoints: every response model that
carries a PO header has it, the /api/v1/po list can filter and sort on it, and
the legacy /purchase view/export search matches it."""
import inspect

import pytest

from app.modules.purchase import po_router
from app.modules.purchase.schemas.header import POHeaderOut
from app.modules.purchase.schemas.po_api import PoListItem, PreviewHeader
from app.modules.purchase.services import po_query
from app.modules.purchase.services.queries import build_where_clause

ADDRESS = "A-185 , MIDC TTC Industrial Area,, Khairane, Navi Mumbai 400709"


@pytest.mark.parametrize("model", [PoListItem, PreviewHeader, POHeaderOut])
def test_response_models_carry_the_field(model):
    # PoListItem: GET /po, /po/{txn}, /po/{txn}/lines header.
    # POHeaderOut: /purchase/upload, /view, /export, /{txn}, /receive, PUT+POST /boxes.
    assert "consignee_address" in model.model_fields


class Row(dict):
    def keys(self):
        return super().keys()


def _row(**extra):
    base = {k: None for k in (
        "transaction_no", "entity", "po_number", "po_date", "voucher_type",
        "order_reference_no", "narration", "vendor_supplier_name", "gross_total",
        "total_amount", "sgst_amount", "cgst_amount", "igst_amount", "round_off",
        "freight_transport_local", "apmc_tax", "packing_charges",
        "freight_transport_charges", "loading_unloading_charges",
        "other_charges_non_gst")}
    base.update(transaction_no="TR-20261022120000", entity="cfpl", **extra)
    return Row(base)


def test_list_item_projects_the_address():
    item = po_query.header_row_to_listitem(_row(consignee_address=ADDRESS))
    assert item["consignee_address"] == ADDRESS
    assert PoListItem(**item).consignee_address == ADDRESS


def test_list_item_tolerates_a_db_without_the_column():
    assert po_query.header_row_to_listitem(_row())["consignee_address"] is None


def test_list_filter_contains():
    where, params = po_query.build_where(
        filters={"consignee_address_contains": "A-185"},
        include_deleted=False, entity_scope=None)
    assert "consignee_address ILIKE $1" in where
    assert params == ["%A-185%"]


def test_list_endpoint_exposes_the_filter():
    assert "consignee_address_contains" in inspect.signature(po_router.list_pos).parameters


def test_list_sort():
    assert po_query.parse_sort("consignee_address:asc") == ("consignee_address", "ASC")


def test_legacy_search_matches_the_address():
    where, params = build_where_clause(
        search="Khairane", entity=None, vendor=None, customer=None,
        date_from=None, date_to=None, status=None, warehouse=None,
        item_category=None, sub_category=None, item_type=None)
    assert "h.consignee_address ILIKE $1" in where
    assert params == ["%Khairane%"]
