"""Material In site scope: a warehouse-locked receiving user doesn't see POs
addressed to another warehouse; admins, the purchase team (purchase_manager
role) and unlocked users see every PO; POs addressed to no site stay visible."""
import asyncio

import pytest
from fastapi import HTTPException

from app.modules.purchase import router as legacy_router
from app.modules.purchase.services import po_query, po_site_scope
from app.modules.purchase.services.consignee_site import CONSIGNEE_SITE_KEY_SQL
from app.modules.purchase.services.queries import build_where_clause

W202 = "W-202(A), MIDC,, TTC Industrial Area, Khairne,, Navi Mumbai - 400710"
A185 = "A-185 , MIDC TTC Industrial Area,, Khairane, Thane Belapur Road"


class User:
    def __init__(self, *, is_admin=False, warehouses=(), role_ids=(7,)):
        self.is_admin = is_admin
        self.allowed_warehouses = list(warehouses)
        self.role_ids = list(role_ids)


@pytest.fixture
def can_create():
    """Role ids that are purchase_manager (the purchase team)."""
    return PURCHASE_ROLE_IDS


def _hidden(user):
    return asyncio.run(po_site_scope.hidden_sites_for(Pool(None), user))


def test_admin_sees_every_site(can_create):
    assert _hidden(User(is_admin=True, warehouses=["W202"])) == []


def test_user_without_a_warehouse_lock_sees_every_site(can_create):
    assert _hidden(User(warehouses=[])) == []


def test_purchase_team_sees_every_site(can_create):
    can_create.add(7)
    assert _hidden(User(warehouses=["W202"])) == []


def test_receiving_user_is_limited_to_their_warehouse(can_create):
    hidden = _hidden(User(warehouses=["W-202"]))
    assert "W202" not in hidden
    for other in ("A185", "A101", "A68", "F53"):
        assert other in hidden
    assert "WAREHOUSEA185" in hidden          # every spelling of a hidden site


def test_two_warehouses(can_create):
    hidden = _hidden(User(warehouses=["W202", "A185"]))
    assert "W202" not in hidden and "A185" not in hidden and "A68" in hidden


def test_address_hidden():
    hidden = ["A185", "A68"]
    assert po_site_scope.address_hidden(A185, hidden)
    assert not po_site_scope.address_hidden(W202, hidden)
    assert not po_site_scope.address_hidden(None, hidden)          # no address: visible
    assert not po_site_scope.address_hidden("Plot 12, MIDC", hidden)
    assert not po_site_scope.address_hidden(A185, [])


def test_list_where_leaves_out_hidden_sites():
    where, params = po_query.build_where(
        filters={}, include_deleted=False, entity_scope=None, hidden_sites=["A185", "A68"])
    assert f"NOT COALESCE({CONSIGNEE_SITE_KEY_SQL} = ANY($1::text[]), FALSE)" in where
    assert params == [["A185", "A68"]]


def test_list_where_unscoped_by_default():
    where, _ = po_query.build_where(filters={}, include_deleted=False, entity_scope=None)
    assert CONSIGNEE_SITE_KEY_SQL not in where


def test_legacy_where_leaves_out_hidden_sites():
    where, params = build_where_clause(
        search=None, entity=None, vendor=None, customer=None, date_from=None,
        date_to=None, status=None, warehouse=None, item_category=None,
        sub_category=None, item_type=None, hidden_sites=["A68"])
    assert "regexp_match(h.consignee_address" in where
    assert "ANY($1::text[])" in where and params == [["A68"]]


PURCHASE_ROLE_IDS: set[int] = set()


@pytest.fixture(autouse=True)
def _reset_roles():
    PURCHASE_ROLE_IDS.clear()


class Pool:
    def __init__(self, address):
        self.address = address

    async def fetchval(self, sql, *args):
        if "FROM auth_role" in sql:
            role_ids, role_name = args
            assert role_name == "purchase_manager"
            return 1 if PURCHASE_ROLE_IDS & set(role_ids) else None
        assert "consignee_address" in sql
        return self.address

    def acquire(self):
        pool = self

        class Acq:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *a):
                return False
        return Acq()


def _assert_visible(address, user):
    asyncio.run(legacy_router._assert_site_visible(Pool(address), user, "TR-1"))


def test_receiving_endpoints_404_another_sites_po(can_create):
    with pytest.raises(HTTPException) as e:
        _assert_visible(A185, User(warehouses=["W202"]))
    assert e.value.status_code == 404


def test_receiving_endpoints_allow_own_and_unaddressed_pos(can_create):
    _assert_visible(W202, User(warehouses=["W202"]))
    _assert_visible(None, User(warehouses=["W202"]))
    _assert_visible(A185, User(is_admin=True))
