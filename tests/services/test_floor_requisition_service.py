"""requisition_service against a scripted fake connection.

The fake answers each statement by what it reads from, so these tests pin the
decisions the service makes (place, unit, snapshot, scope, refusals, guarded
transitions) without a database. The SQL runs for real only on RDS.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg
import pytest
from fastapi import HTTPException

from app.modules.floor_requisition.services import requisition_service as svc

COLS = [c.strip() for c in svc.COLS.split(",")]
RAISED_AT = datetime(2026, 9, 15, 8, 35, tzinfo=timezone.utc)
INSERT_COLS = ["requisition_id", "job_card_id", "warehouse", "floor", "material_sku_name",
               "item_type", "requested_qty", "requested_unit", "required_qty", "required_unit",
               "available_qty", "available_unit", "shortage_qty", "shortage_unit",
               "note", "raised_by"]
PISTA = "California Pista Inshell Roasted and Salted"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, *, job_card=None, bom=None, indents=(), insert_error=None, open_id=None,
                 row=None, update_row=None, status=None, total=0, rows=(), job_cards=(),
                 store_response_installed=False, store_responses=()):
        self.job_card, self.bom, self.indents = job_card, bom, list(indents)
        self.insert_error, self.open_id = insert_error, open_id
        self.row, self.update_row, self.status = row, update_row, status
        self.total, self.rows, self.job_cards = total, list(rows), list(job_cards)
        self.store_response_installed, self.store_responses = store_response_installed, list(store_responses)
        self.calls: list[tuple[str, str, tuple]] = []

    def is_in_transaction(self):
        return True

    def transaction(self):
        return _Tx()

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        return s

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        if s.startswith("SELECT job_card_id, factory, floor, bom_id FROM job_card_v2"):
            return self.job_card
        if "FROM bom_line" in s:
            return self.bom
        if s.startswith("INSERT INTO floor_requisition"):
            if self.insert_error:
                raise self.insert_error
            row = dict.fromkeys(COLS)
            row.update(zip(INSERT_COLS, args))
            row.update(status="raised", raised_at=RAISED_AT)
            return row
        if s.startswith("UPDATE floor_requisition"):
            return self.update_row
        if "FROM floor_requisition" in s:
            return self.row
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if "job_card_rm_indent_v2" in s:
            return self.indents
        if "store_response IS NOT NULL" in s:
            return self.store_responses
        if "FROM floor_requisition" in s:
            return self.rows
        if "FROM job_card_v2" in s:
            return self.job_cards
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        # jc_bom_changes (spec 2f): the plan-line lock, then "is migration 115
        # applied?" -- no, so live_change_for stops there.
        if s.startswith("/* jcbc:lock_card_share */"):
            return 70
        if s.startswith("/* jcbc:table */"):
            return False
        if "information_schema.columns" in s:
            return self.store_response_installed
        if s.startswith("SELECT COUNT(*) FROM floor_requisition"):
            return self.total
        if s.startswith("SELECT requisition_id FROM floor_requisition"):
            return self.open_id
        if s.startswith("SELECT status FROM floor_requisition"):
            return self.status
        raise AssertionError(f"unexpected fetchval: {s[:90]}")


class User:
    def __init__(self, full_name="Ravi K", warehouses=(), floors=()):
        self.full_name, self.email, self.phone, self.user_id = full_name, "", "", 7
        self.allowed_warehouses, self.allowed_floors = list(warehouses), list(floors)


def _jc(**over):
    return {"job_card_id": 12345678, "factory": "W-202", "floor": " First Floor ", "bom_id": 9, **over}


def _row(**over):
    row = dict.fromkeys(COLS)
    row.update(requisition_id=87654321, job_card_id=12345678, warehouse="W202", floor="First Floor",
               material_sku_name="PM24-Pouch 250 gm", item_type="PM",
               requested_qty=Decimal("1000.000"), requested_unit="pcs",
               available_qty=Decimal("0.000"), available_unit="pcs",
               status="raised", raised_by="Floor F", raised_at=RAISED_AT)
    row.update(over)
    return row


@pytest.fixture
def floor_stock(monkeypatch):
    seen = {}

    async def fake(conn, *, warehouse, floor):
        seen["place"] = (warehouse, floor)
        return {"warehouse": warehouse, "floor": floor, "items": seen.get("items", [])}

    monkeypatch.setattr(svc.floor_stock_service, "fetch_floor_stock", fake)
    return seen


def _raise(conn, user=None, **kw):
    kw.setdefault("job_card_id", 12345678)
    kw.setdefault("material_sku_name", PISTA)
    kw.setdefault("requested_qty", "88.2")
    kw.setdefault("note", None)
    return asyncio.run(svc.raise_requisition(conn, user or User(), **kw))


def _refusal(fn):
    with pytest.raises(svc.RequisitionError) as exc:
        fn()
    return exc.value


# ── raise ──────────────────────────────────────────────────────────────────

def test_raise_takes_place_unit_and_snapshot_from_the_database(floor_stock):
    floor_stock["items"] = [
        {"item_name": PISTA.upper() + " ", "stock_type": "Fresh Stock", "available_kg": 161.8, "available_quantity": 0},
        {"item_name": PISTA, "stock_type": "Off Grade/Rejection", "available_kg": 2.89, "available_quantity": 0},
        {"item_name": "Walnut", "stock_type": "Fresh Stock", "available_kg": 999, "available_quantity": 0},
    ]
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "rm"},
                    indents=[{"item_type": "RM", "material_sku_name": PISTA, "uom": "KGS",
                              "gross_qty": Decimal("250.000"), "reqd_qty": Decimal("240"), "line_id": 1}])
    out = _raise(conn, material_sku_name=f"  {PISTA.lower()} ", note="  urgent ")

    assert floor_stock["place"] == ("W202", "First Floor")
    assert out["warehouse"] == "W202" and out["floor"] == "First Floor"
    assert out["material_sku_name"] == PISTA and out["item_type"] == "RM"
    assert (out["requested_qty"], out["requested_unit"]) == (88.2, "kg")
    assert (out["required_qty"], out["required_unit"]) == (250.0, "kg")
    assert (out["available_qty"], out["available_unit"]) == (161.8, "kg")
    assert (out["shortage_qty"], out["shortage_unit"]) == (88.2, "kg")
    assert out["note"] == "urgent" and out["raised_by"] == "Ravi K"
    assert out["status"] == "raised" and out["raised_at"] == RAISED_AT.isoformat()
    bom_call = next(c for c in conn.calls if "FROM bom_line" in c[1])
    assert bom_call[2] == (9, PISTA.upper())


def test_raise_mints_the_8_digit_number_through_the_pk_retry(floor_stock, monkeypatch):
    used = []

    async def spy(conn, insert):
        used.append(insert)
        return await insert()

    monkeypatch.setattr(svc, "insert_with_pk_retry", spy)
    monkeypatch.setattr(svc, "new_short_time_id", lambda: 12349876)
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "RM"})
    assert _raise(conn)["requisition_id"] == 12349876
    assert len(used) == 1


def test_raise_for_a_missing_job_card_is_404(floor_stock):
    err = _refusal(lambda: _raise(FakeConn(job_card=None)))
    assert (err.status, err.error) == (404, "job_card_not_found")


def test_raise_on_a_job_card_without_a_floor_is_422(floor_stock):
    err = _refusal(lambda: _raise(FakeConn(job_card=_jc(floor=None))))
    assert (err.status, err.error) == (422, "job_card_has_no_place")


def test_raise_outside_the_callers_warehouses_is_403(floor_stock):
    with pytest.raises(HTTPException) as exc:
        _raise(FakeConn(job_card=_jc()), User(warehouses=["A185"]))
    assert exc.value.status_code == 403


def test_raise_for_an_article_not_on_the_job_card_is_422_and_reads_no_stock(floor_stock):
    err = _refusal(lambda: _raise(FakeConn(job_card=_jc(), bom=None, indents=[])))
    assert (err.status, err.error) == (422, "article_not_on_job_card")
    assert "place" not in floor_stock


def test_raise_pm_without_an_indent_line_is_in_pieces(floor_stock):
    floor_stock["items"] = [{"item_name": "PM24-Pouch 250 gm", "stock_type": "Fresh Stock",
                             "available_kg": 6.0, "available_quantity": 120}]
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": "PM24-Pouch 250 gm", "item_type": "PM"})
    err = _refusal(lambda: _raise(conn, material_sku_name="PM24-Pouch 250 gm", requested_qty="10.5"))
    assert (err.status, err.error) == (400, "qty_invalid")
    assert err.message == "Pieces are whole numbers."

    out = _raise(conn, material_sku_name="PM24-Pouch 250 gm", requested_qty="1000")
    assert (out["requested_qty"], out["requested_unit"]) == (1000.0, "pcs")
    assert (out["available_qty"], out["available_unit"]) == (120.0, "pcs")
    assert out["required_qty"] is None and out["required_unit"] is None
    assert out["shortage_qty"] is None and out["shortage_unit"] is None


def test_a_second_open_request_for_the_article_is_409_naming_the_first(floor_stock):
    clash = asyncpg.UniqueViolationError(
        'duplicate key value violates unique constraint "uq_floor_requisition_open"')
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "RM"},
                    insert_error=clash, open_id=87654321)
    err = _refusal(lambda: _raise(conn))
    assert (err.status, err.error) == (409, "open_requisition_exists")
    assert err.details["requisition_id"] == 87654321


def test_any_other_unique_violation_is_not_swallowed(floor_stock):
    other = asyncpg.UniqueViolationError('duplicate key value violates unique constraint "some_other_idx"')
    conn = FakeConn(job_card=_jc(), bom={"material_sku_name": PISTA, "item_type": "RM"}, insert_error=other)
    with pytest.raises(asyncpg.UniqueViolationError):
        _raise(conn)


# ── issue / receive / cancel ───────────────────────────────────────────────

def _update_call(conn):
    return next(c for c in conn.calls if c[1].startswith("UPDATE floor_requisition"))


def test_issue_moves_raised_to_issued_in_the_requested_unit():
    conn = FakeConn(row=_row(), update_row=_row(status="issued", issued_qty=Decimal("500.000"),
                                                issued_unit="pcs", issued_by="Store S"))
    out = asyncio.run(svc.issue_requisition(conn, User(full_name="Store S"), 87654321,
                                            issued_qty="500", issue_note=" part "))
    assert out["status"] == "issued" and out["issued_qty"] == 500.0
    sql, args = _update_call(conn)[1:]
    assert "issued_unit = requested_unit" in sql and "status = $2" in sql
    assert args == (87654321, "raised", Decimal("500.000"), "part", "Store S")


def test_issue_quantity_follows_the_unit_rules():
    err = _refusal(lambda: asyncio.run(svc.issue_requisition(
        FakeConn(row=_row()), User(), 87654321, issued_qty="12.5", issue_note=None)))
    assert (err.status, err.error) == (400, "qty_invalid")


def test_a_request_someone_already_moved_on_is_409_with_its_status():
    conn = FakeConn(row=_row(), update_row=None, status="issued")
    err = _refusal(lambda: asyncio.run(svc.issue_requisition(conn, User(), 87654321, issued_qty="5", issue_note=None)))
    assert (err.status, err.error) == (409, "status_changed")
    assert err.details["status"] == "issued"


def test_a_missing_requisition_is_404():
    err = _refusal(lambda: asyncio.run(svc.receive_requisition(FakeConn(row=None), User(), 1)))
    assert (err.status, err.error) == (404, "not_found")


def test_acting_on_another_floors_request_is_403():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.receive_requisition(FakeConn(row=_row()), User(floors=["Terrace"]), 87654321))
    assert exc.value.status_code == 403


def test_receive_moves_issued_to_received():
    conn = FakeConn(row=_row(status="issued"), update_row=_row(status="received", received_by="Ravi K"))
    out = asyncio.run(svc.receive_requisition(conn, User(), 87654321))
    assert out["status"] == "received"
    assert _update_call(conn)[2] == (87654321, "issued", "Ravi K")


def test_cancel_needs_a_reason():
    err = _refusal(lambda: asyncio.run(svc.cancel_requisition(FakeConn(row=_row()), User(), 87654321, reason="   ")))
    assert (err.status, err.error) == (400, "reason_required")


def test_cancel_moves_raised_to_cancelled():
    conn = FakeConn(row=_row(), update_row=_row(status="cancelled", cancel_reason="wrong article"))
    asyncio.run(svc.cancel_requisition(conn, User(), 87654321, reason=" wrong article "))
    assert _update_call(conn)[2] == (87654321, "raised", "wrong article", "Ravi K")


# ── list ───────────────────────────────────────────────────────────────────

def test_list_is_limited_to_the_callers_places_and_filters():
    conn = FakeConn(total=1, rows=[_row()])
    out = asyncio.run(svc.list_requisitions(conn, User(warehouses=["W-202"], floors=["first floor"]),
                                            status="raised", search="pouch 100%", page_size=9999))
    count_sql, count_args = next((c[1], c[2]) for c in conn.calls if "COUNT(*)" in c[1])
    assert "warehouse = ANY($1)" in count_sql and "UPPER(floor) = ANY($2)" in count_sql
    assert "status = $3" in count_sql
    assert count_args == (["W202"], ["FIRST FLOOR"], "raised", "%POUCH%", "%100\\%%")
    list_sql, list_args = next((c[1], c[2]) for c in conn.calls if c[0] == "fetch")
    assert "ORDER BY raised_at DESC, requisition_id DESC" in list_sql
    assert list_args[-2:] == (500, 0)
    assert out["total"] == 1 and out["page"] == 1 and out["page_size"] == 500
    assert out["items"][0]["requested_qty"] == 1000.0


def test_list_for_one_job_card_without_limits():
    conn = FakeConn(total=0, rows=[])
    asyncio.run(svc.list_requisitions(conn, User(), job_card_id=12345678, page=3, page_size=100))
    count_sql, count_args = next((c[1], c[2]) for c in conn.calls if "COUNT(*)" in c[1])
    assert count_sql.endswith("WHERE job_card_id = $1") and count_args == (12345678,)
    assert next(c[2] for c in conn.calls if c[0] == "fetch")[-2:] == (100, 200)


def _card(**over):
    return {"job_card_id": 12345678, "job_card_number": "PLAN-41-L2-S1",
            "fg_sku_name": "Pista Roasted & Salted 200 g", "customer_name": "Reliance Retail",
            "batch_number": "P41-L2-S1", "process_name": "Roasting", "stage": "stage_1",
            "status": "in_progress", "entity": "cfpl", **over}


def test_list_carries_each_rows_job_card_in_one_extra_read():
    rows = [_row(), _row(requisition_id=11112222), _row(requisition_id=33334444, job_card_id=99990000)]
    conn = FakeConn(total=3, rows=rows, job_cards=[_card()])
    out = asyncio.run(svc.list_requisitions(conn, User()))
    card_reads = [c for c in conn.calls if c[0] == "fetch" and "FROM job_card_v2" in c[1]]
    assert len(card_reads) == 1
    assert card_reads[0][2] == ([12345678, 99990000],)
    first, second, orphan = out["items"]
    assert first["job_card"] == _card() and second["job_card"] == _card()
    # A job card the read did not return is None, never a KeyError or a made-up record.
    assert orphan["job_card"] is None
    # The requisition's own columns are untouched by the join.
    assert first["floor"] == "First Floor" and first["status"] == "raised"


def test_an_empty_page_reads_no_job_cards():
    conn = FakeConn(total=0, rows=[])
    out = asyncio.run(svc.list_requisitions(conn, User(), status="issued"))
    assert out["items"] == []
    assert not [c for c in conn.calls if "FROM job_card_v2" in c[1]]



def test_list_carries_stores_whatsapp_reply_once_migration_112_has_run():
    at = datetime(2026, 9, 17, 7, 30, tzinfo=timezone.utc)
    rows = [_row(), _row(requisition_id=11112222)]
    conn = FakeConn(total=2, rows=rows, store_response_installed=True, store_responses=[
        {"requisition_id": 87654321, "store_response": "on_hold", "store_response_by": "Kaushal Patil",
         "store_response_at": at}])
    first, second = asyncio.run(svc.list_requisitions(conn, User()))["items"]
    assert first["store_response"] == {"response": "on_hold", "by": "Kaushal Patil", "at": at.isoformat()}
    assert second["store_response"] is None
    read = next(c for c in conn.calls if "store_response IS NOT NULL" in c[1])
    assert read[2] == ([11112222, 87654321],)


def test_list_works_before_migration_112_without_reading_the_columns():
    conn = FakeConn(total=1, rows=[_row()], store_response_installed=False)
    item = asyncio.run(svc.list_requisitions(conn, User()))["items"][0]
    assert item["store_response"] is None
    assert not [c for c in conn.calls if "store_response IS NOT NULL" in c[1]]
