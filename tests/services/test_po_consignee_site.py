"""Material In's site section: a PO's plant comes from the start of its consignee
address, and the list's section filter partitions on it."""
import pytest

from app.modules.purchase.services import consignee_site as cs
from app.modules.purchase.services import po_query

# The addresses in the Oct 2026 PO Book (22102026POBOOK.xlsx).
W202 = ("W-202(A), MIDC,, TTC Industrial Area, Khairne,, Navi Mumbai - 400710, "
        "FSSAI NO. 10016022005363, FSSAI NO. 11521998000014, E-Mail : contact@candorfoods.in")
A185 = ("A-185 , MIDC TTC Industrial Area,, Khairane, Thane Belapur Road,, "
        "Navi Mumbai 400709, FSSAI NO. 10016022005363, FSSAI NO. 11521998000014")
A68 = ("A-68 , Plot No-68,, Ground Floor TTC Industrial Area,, MIDC Mahape,, "
       "Navi Mumbai, Thane, Maharashtra")
W202_PLOT = "Plot No. W202A, TTC Industrial Area,, Khairne MIDC,  400710"


@pytest.mark.parametrize("address,site", [
    (W202, "W202"), (A185, "A185"), (A68, "A68"), (W202_PLOT, "W202"),
    ("  w 202, somewhere", "W202"),
])
def test_plant_code_at_the_start_names_the_site(address, site):
    assert cs.consignee_site(address) == site


@pytest.mark.parametrize("address", [
    None, "", "Plot 12, MIDC Turbhe", "Shop No 5, Vashi",
    "Z-999, nowhere",            # letter + number, but no such site
    "MIDC, A-185",               # the code has to open the address
])
def test_no_site(address):
    assert cs.consignee_site(address) is None


def test_sql_uses_the_same_pattern():
    # Verified against RDS Postgres 17: the four real forms give W202 / A185 /
    # A68 / W202, and the non-plant addresses give NULL.
    assert f"'{cs._PATTERN}', 'i'" in cs.CONSIGNEE_SITE_KEY_SQL
    assert "${" not in cs.CONSIGNEE_SITE_KEY_SQL     # build_where's placeholder


def _where(**filters):
    return po_query.build_where(filters=filters, include_deleted=False, entity_scope=None)


def test_site_section_matches_the_sites_and_skips_received():
    where, params = _where(section="site", site="W202,A185")
    assert f"COALESCE({cs.CONSIGNEE_SITE_KEY_SQL} = ANY($1::text[]), FALSE)" in where
    assert "NOT (EXISTS (SELECT 1 FROM po_box" in where
    assert "W202" in params[0] and "A185" in params[0]


def test_site_filter_takes_every_spelling_of_a_site():
    _, params = _where(section="site", site="Warehouse A185")
    assert "A185" in params[0]


def test_site_section_without_sites_is_empty():
    where, _ = _where(section="site", site="")
    assert "FALSE" in where.split(" AND ")


def test_pending_leaves_the_sites_out():
    where, params = _where(section="pending", site="W202")
    assert f"NOT COALESCE({cs.CONSIGNEE_SITE_KEY_SQL} = ANY($1::text[]), FALSE)" in where
    assert "NOT (EXISTS" in where
    assert "po_date" not in where


def test_completed_is_every_received_po():
    where, params = _where(section="completed", site="W202")
    assert cs.CONSIGNEE_SITE_KEY_SQL not in where
    assert "(EXISTS (SELECT 1 FROM po_box" in where and "NOT (EXISTS" not in where
    assert params == []


def test_older_client_date_partition_still_works():
    where, params = _where(section="pending", today_date="2026-10-22")
    assert "po_date IS DISTINCT FROM $1" in where
    where, _ = _where(section="today", today_date="2026-10-22")
    assert "po_date = $1" in where


def test_bad_section_is_rejected():
    with pytest.raises(Exception):
        _where(section="tomorrow")


def test_list_item_carries_the_site():
    from tests.services.test_po_consignee_address_endpoints import _row
    item = po_query.header_row_to_listitem(_row(consignee_address=A68))
    assert item["consignee_site"] == "A68"
