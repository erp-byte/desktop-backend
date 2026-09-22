"""box_service against a scripted fake connection: which table a scan resolves
from, what is refused, what is written. The SQL runs for real only on a DB."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.modules.floor_requisition.services import box_service as bs
from app.modules.floor_requisition.services import requisition_service as svc

REQ_COLS = [c.strip() for c in svc.COLS.split(",")]
AT = datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc)
SEEDS = "Sunflower Seeds Roasted"


class User:
    def __init__(self, warehouses=(), floors=()):
        self.full_name, self.email, self.phone, self.user_id = "Store Sam", "", "", 9
        self.allowed_warehouses, self.allowed_floors = list(warehouses), list(floors)


def _req(**over):
    base = {c: None for c in REQ_COLS}
    base.update(requisition_id=27385955, job_card_id=75009889, warehouse="A185", floor="Mezzanine",
                material_sku_name=SEEDS, status="issued")
    base.update(over)
    return base


def _box_row(**over):
    row = {"requisition_id": 27385955, "box_code": "BX-1", "source": "scanned", "box_table": "po_box",
           "box_number": None, "transaction_no": "TR-1", "article": SEEDS, "stock_type": None,
           "lot_number": None, "net_weight": Decimal("10.500"), "gross_weight": Decimal("11.000"),
           "count": 4, "recorded_by": "Store Sam", "recorded_at": AT}
    row.update(over)
    return row


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, *, req=None, dup=None, dup_after="same", sfg=None, po=(), inserted="auto", status_after="issued",
                 rows=(), locked_status=None, source=None, jc=None, last=0, in_jc=False,
                 jc_table=True, sfg_delete="DELETE 1", sfg_still_there=False, taken=(),
                 summary=None, by_article=(), found=None):
        self.req = req if req is not None else _req()
        self.dup, self.sfg, self.po = dup, sfg, list(po)
        self.dup_after = dup if dup_after == "same" else dup_after
        self.dup_calls = 0
        self.inserted, self.status_after, self.rows = inserted, status_after, list(rows)
        self.locked_status = locked_status
        self.source, self.jc, self.last = source, jc, last
        self.in_jc, self.jc_table = in_jc, jc_table
        self.sfg_delete, self.sfg_still_there, self.taken = sfg_delete, sfg_still_there, list(taken)
        self.summary = summary or {"boxes": 0, "net_weight": Decimal("0"), "gross_weight": Decimal("0"),
                                   "count": 0, "last_box_number": 0}
        self.by_article, self.found = list(by_article), found
        self.calls: list[tuple[str, str, tuple]] = []
        self.sfg_inserts: list[tuple] = []
        self.box_inserts: list[tuple] = []

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
        if s.startswith("SELECT requisition_id, job_card_id"):
            return self.req
        if s.startswith("SELECT carton_id, fg_sku_name"):
            return self.sfg
        if s.startswith("INSERT INTO floor_requisition_box") and "'scanned'" in s:
            if self.inserted == "auto":
                return _box_row(box_code=args[1], box_table=args[2], transaction_no=args[3],
                                article=args[4], lot_number=args[5], net_weight=args[6],
                                gross_weight=args[7], count=args[8], recorded_by=args[9])
            return self.inserted
        if s.startswith("INSERT INTO floor_requisition_box") and "'printed'" in s:
            self.box_inserts.append(args)
            return _box_row(box_code=args[1], source="printed", box_table="sfg_box", box_number=args[2],
                            transaction_no=args[3], article=args[4], stock_type=args[5], lot_number=args[6],
                            net_weight=args[7], gross_weight=args[8], count=args[9], recorded_by=args[10])
        if s.startswith("SELECT job_card_number, entity FROM job_card_v2"):
            return self.jc
        if s.startswith("SELECT b.requisition_id, fr.job_card_id, jc.job_card_number"):
            self.dup_calls += 1
            return self.dup if self.dup_calls == 1 else self.dup_after
        if s.startswith("SELECT count(*) AS boxes"):
            return self.summary
        if s.startswith("SELECT box_code, pos FROM"):
            return self.found
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if s.startswith("SELECT b.box_id, b.transaction_no"):
            return self.po
        if s.startswith("SELECT requisition_id, box_code"):
            return self.rows
        if s.startswith("SELECT box_number FROM floor_requisition_box"):
            return [{"box_number": n} for n in self.taken]
        if s.startswith("SELECT article, COALESCE(sum(net_weight), 0)"):
            return self.by_article
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        if s.startswith("SELECT status FROM floor_requisition WHERE requisition_id = $1 FOR UPDATE"):
            return self.locked_status or self.req["status"]
        if s.startswith("SELECT status FROM floor_requisition"):
            return self.status_after
        if s.startswith("SELECT COALESCE(MAX(CAST(split_part(carton_id"):
            return self.last
        if s.startswith("INSERT INTO sfg_box"):
            self.sfg_inserts.append(args)
            return args[0]
        if s.startswith("SELECT source FROM floor_requisition_box"):
            return self.source
        if s.startswith("SELECT to_regclass('jc_box_scan')"):
            return self.jc_table
        if s.startswith("SELECT 1 FROM jc_box_scan"):
            return 1 if self.in_jc else None
        if s.startswith("SELECT 1 FROM sfg_box"):
            return 1 if self.sfg_still_there else None
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def execute(self, sql, *args):
        s = self._log("execute", sql, args)
        if s.startswith("DELETE FROM sfg_box"):
            return self.sfg_delete
        if s.startswith("DELETE FROM floor_requisition_box"):
            return "DELETE 1"
        raise AssertionError(f"unexpected execute: {s[:90]}")

    def ran(self, prefix):
        return [c for c in self.calls if c[1].startswith(prefix)]


def _identify(result):
    async def fake(conn, value):
        fake.seen = value
        return result
    fake.seen = None
    return fake


NOT_FOUND = {"found": False, "box_id": "X"}


def run(coro):
    return asyncio.run(coro)


def _refusal(exc_info):
    return exc_info.value.status, exc_info.value.error


# ── label parsing ──
@pytest.mark.parametrize("raw, want", [
    ('{"tx":"TR-7","bi":"BX-9"}', ("BX-9", "TR-7")),
    ('  {"tx":" TR-7 ","bi":" BX-9 "} ', ("BX-9", "TR-7")),
    ('{"tx":27385955,"bi":"48213307-1"}', ("48213307-1", "27385955")),
    ('{"bi":"BX-9"}', ("BX-9", None)),
    ('{"tx":"  ","bi":"BX-9"}', ("BX-9", None)),
    ("  48213307-2  ", ("48213307-2", None)),
    ("42", ("42", None)),
    ('{"x":1}', ('{"x":1}', None)),
    ("", ("", None)),
])
def test_parse_label(raw, want):
    assert bs.parse_label(raw) == want


# ── lookup order ──
def test_a_bare_id_is_looked_up_in_sfg_box_first(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(sfg={"carton_id": "48213307-1", "fg_sku_name": SEEDS, "sfg_code": SEEDS,
                         "batch_code": "L-1", "net_weight": Decimal("9.770"),
                         "gross_weight": Decimal("10.000"), "units": 3, "status": "PRINTED"})
    box = run(bs.resolve_box(conn, "48213307-1"))
    assert box == {"box_code": "48213307-1", "box_table": "sfg_box", "transaction_no": None,
                   "article": SEEDS, "lot_number": "L-1", "net_weight": Decimal("9.770"),
                   "gross_weight": Decimal("10.000"), "count": 3}
    assert not conn.ran("SELECT b.box_id")


def test_a_label_with_tx_tries_po_box_first_and_matches_its_transaction(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=[{"box_id": "BX-9", "transaction_no": "TR-7", "lot_number": "L9",
                         "net_weight": Decimal("25.000"), "gross_weight": Decimal("25.400"),
                         "count": 1, "sku_name": SEEDS}])
    box = run(bs.resolve_box(conn, '{"tx":"TR-7","bi":"BX-9"}'))
    assert box["box_table"] == "po_box" and box["transaction_no"] == "TR-7" and box["article"] == SEEDS
    assert conn.ran("SELECT b.box_id")[0][2] == ("BX-9", "TR-7")
    assert not conn.ran("SELECT carton_id")


def test_a_bare_id_on_two_purchase_orders_is_ambiguous(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    two = [{"box_id": "BX-9", "transaction_no": t, "lot_number": None, "net_weight": 1,
            "gross_weight": None, "count": None, "sku_name": SEEDS} for t in ("TR-1", "TR-2")]
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(po=two), "BX-9"))
    assert _refusal(e) == (409, "ambiguous_box")


def test_the_wider_identify_is_the_last_step(monkeypatch):
    fake = _identify({"found": True, "table": "cfpl_cold_stocks", "box": {
        "box_id": "CS-1", "transaction_no": "CT-3", "item_description": "Almonds",
        "lot_number": "LX", "net_weight": 20.0, "gross_weight": None, "count": None}})
    monkeypatch.setattr(bs, "identify_box", fake)
    box = run(bs.resolve_box(FakeConn(), "CS-1"))
    assert fake.seen == "CS-1"
    assert box == {"box_code": "CS-1", "box_table": "cfpl_cold_stocks", "transaction_no": "CT-3",
                   "article": "Almonds", "lot_number": "LX", "net_weight": 20.0,
                   "gross_weight": None, "count": None}


def test_identify_ambiguity_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(
        {"found": True, "ambiguous": True, "table": "po_box", "also_in": ["cfpl_boxes_v2"], "box": {}}))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(), "BX-1"))
    assert _refusal(e) == (409, "ambiguous_box")
    assert e.value.details["tables"] == ["po_box", "cfpl_boxes_v2"]


def test_an_unknown_box_points_to_manual_print(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(), "NOPE-1"))
    assert _refusal(e) == (404, "box_not_found")
    assert "Manual print" in e.value.message


def test_a_cancelled_sfg_box_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(sfg={"carton_id": "S-1", "fg_sku_name": None, "sfg_code": "SFG0001", "batch_code": None,
                         "net_weight": 1, "gross_weight": None, "units": None, "status": "CANCELLED"})
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(conn, "S-1"))
    assert _refusal(e) == (409, "box_cancelled")


def test_a_qr_without_a_box_id_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.resolve_box(FakeConn(), '{"tx":"TR-1","bi":"  "}'))
    assert _refusal(e) == (400, "no_box_id")


# ── scan ──
def _po_hit():
    return [{"box_id": "BX-1", "transaction_no": "TR-1", "lot_number": None, "net_weight": Decimal("10.500"),
             "gross_weight": Decimal("11.000"), "count": 4, "sku_name": SEEDS}]


def test_scan_records_the_box_with_what_the_lookup_found(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=_po_hit())
    out = run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    args = conn.ran("INSERT INTO floor_requisition_box")[0][2]
    assert args == (27385955, "BX-1", "po_box", "TR-1", SEEDS, None, Decimal("10.500"),
                    Decimal("11.000"), 4, "Store Sam")
    assert out["box_code"] == "BX-1" and out["net_weight"] == 10.5 and out["article_mismatch"] is False
    assert out["recorded_at"] == AT.isoformat()


def test_scan_flags_a_different_article(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    hit = _po_hit()
    hit[0]["sku_name"] = "Pumpkin Seeds"
    out = run(bs.scan_box(FakeConn(po=hit), User(), 27385955, code="BX-1"))
    assert out["article_mismatch"] is True


def test_article_match_ignores_case_and_spacing():
    row = _box_row(article="  sunflower   SEEDS roasted ")
    assert bs.box_out(row, SEEDS)["article_mismatch"] is False


def _sent(requisition_id=27385955, job_card_id=75009889, job_card_number="PLAN-7-L1-S2"):
    return {"requisition_id": requisition_id, "job_card_id": job_card_id, "job_card_number": job_card_number}


def test_the_scans_table_is_checked_first_across_every_request(monkeypatch):
    fake = _identify(NOT_FOUND)
    monkeypatch.setattr(bs, "identify_box", fake)
    conn = FakeConn(dup=_sent())
    with pytest.raises(svc.RequisitionError):
        run(bs.scan_box(conn, User(), 27385955, code='{"tx":"TR-1","bi":"BX-1"}'))
    kind, sql, args = conn.ran("SELECT b.requisition_id, fr.job_card_id")[0]
    assert args == ("BX-1",)                      # the box alone — no request filter
    assert "WHERE b.box_code = $1" in sql and "requisition_id = $" not in sql.split("WHERE", 1)[1]
    # Nothing else was read: no box table, no identify.
    assert fake.seen is None and not conn.ran("SELECT b.box_id") and not conn.ran("SELECT carton_id")


def test_a_box_already_on_this_request_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(FakeConn(dup=_sent()), User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "duplicate_box")
    assert e.value.message == "Box BX-1 is already on this request."


def test_a_box_sent_on_another_request_is_refused_and_named(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(dup=_sent(requisition_id=11112222, job_card_id=5, job_card_number="PLAN-9-L1-S1"))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "duplicate_box")
    assert e.value.message == "Box BX-1 was already sent on request #11112222 (job card PLAN-9-L1-S1)."
    assert e.value.details == {"requisition_id": 27385955, "box_code": "BX-1", "sent_on_requisition_id": 11112222}


def test_without_a_job_card_number_the_id_is_named(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(dup=_sent(requisition_id=11112222, job_card_id=5, job_card_number=None))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert e.value.message == "Box BX-1 was already sent on request #11112222 (job card 5)."


def test_scan_only_while_issued(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(FakeConn(req=_req(status="received")), User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "not_issued")


def test_scan_outside_the_callers_place_is_403(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    with pytest.raises(HTTPException) as e:
        run(bs.scan_box(FakeConn(), User(warehouses=["W202"]), 27385955, code="BX-1"))
    assert e.value.status_code == 403


def test_a_lost_insert_race_names_where_the_box_went(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=_po_hit(), inserted=None, dup=None, dup_after=_sent(requisition_id=11112222))
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "duplicate_box")
    assert "request #11112222" in e.value.message


def test_a_lost_race_whose_winner_is_gone_still_refuses(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=_po_hit(), inserted=None, dup=None, dup_after=None)
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "duplicate_box")


def test_a_request_moved_on_mid_scan_reports_its_status(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    conn = FakeConn(po=_po_hit(), inserted=None, status_after="received")
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert _refusal(e) == (409, "not_issued")


def test_a_box_without_an_article_is_recorded_as_unknown(monkeypatch):
    monkeypatch.setattr(bs, "identify_box", _identify(NOT_FOUND))
    hit = _po_hit()
    hit[0]["sku_name"] = None
    conn = FakeConn(po=hit)
    run(bs.scan_box(conn, User(), 27385955, code="BX-1"))
    assert conn.ran("INSERT INTO floor_requisition_box")[0][2][4] == bs.UNKNOWN_ARTICLE


def test_the_insert_is_guarded_on_issued_and_on_conflict():
    sql = " ".join(bs._INSERT_SCANNED_SQL.split())
    assert "WHERE EXISTS (SELECT 1 FROM floor_requisition WHERE requisition_id = $1 AND status = 'issued')" in sql
    # Any clash — this request's key or the box id across requests (migration 114).
    assert "ON CONFLICT DO NOTHING" in sql


# ── list: one page, request-wide figures, find ──
def _summary(boxes, net="0", gross="0", count=0, last=0):
    return {"boxes": boxes, "net_weight": Decimal(net), "gross_weight": Decimal(gross), "count": count,
            "last_box_number": last}


def _page_args(conn):
    return conn.ran("SELECT requisition_id, box_code")[-1][2]


def test_list_returns_one_page_and_request_wide_figures_for_any_status():
    rows = [_box_row(box_code="A", net_weight=Decimal("1.250"), gross_weight=None, count=None),
            _box_row(box_code="B", net_weight=Decimal("2.000"), gross_weight=Decimal("2.5"), count=3)]
    conn = FakeConn(req=_req(status="received"), rows=rows, summary=_summary(12, "40.5", "42", 7, last=9),
                    by_article=[{"article": SEEDS, "net_weight": Decimal("38.5"), "boxes": 11},
                                {"article": "Pumpkin Seeds", "net_weight": Decimal("2"), "boxes": 1}])
    out = run(bs.list_boxes(conn, User(), 27385955))
    assert [b["box_code"] for b in out["boxes"]] == ["A", "B"]
    assert out["status"] == "received"
    assert (out["page"], out["page_size"], out["total"], out["pages"]) == (1, 10, 12, 2)
    assert out["totals"] == {"boxes": 12, "net_weight": 40.5, "gross_weight": 42.0, "count": 7}
    assert out["by_article"] == [{"article": SEEDS, "net_weight": 38.5, "boxes": 11},
                                 {"article": "Pumpkin Seeds", "net_weight": 2.0, "boxes": 1}]
    assert out["next_box_number"] == 10
    assert out["found"] is None
    assert _page_args(conn) == (27385955, 10, 0)


def test_a_later_page_is_offset():
    conn = FakeConn(summary=_summary(25))
    out = run(bs.list_boxes(conn, User(), 27385955, page=2, page_size=10))
    assert out["page"] == 2 and _page_args(conn) == (27385955, 10, 10)


def test_a_page_past_the_end_is_the_last_page():
    conn = FakeConn(summary=_summary(25))
    out = run(bs.list_boxes(conn, User(), 27385955, page=9, page_size=10))
    assert (out["page"], out["pages"]) == (3, 3) and _page_args(conn) == (27385955, 10, 20)


def test_an_empty_request_is_one_empty_page():
    conn = FakeConn()
    out = run(bs.list_boxes(conn, User(), 27385955, page=4))
    assert (out["page"], out["pages"], out["total"], out["next_box_number"]) == (1, 1, 0, 1)
    assert _page_args(conn) == (27385955, 10, 0)


@pytest.mark.parametrize("asked, used", [(0, 1), (-5, 1), (1000, bs.MAX_PAGE_SIZE), (25, 25)])
def test_page_size_is_kept_in_bounds(asked, used):
    conn = FakeConn(summary=_summary(300))
    out = run(bs.list_boxes(conn, User(), 27385955, page_size=asked))
    assert out["page_size"] == used and _page_args(conn)[1] == used


def test_the_page_follows_the_list_order():
    assert "ORDER BY recorded_at DESC, box_number DESC NULLS LAST, box_code" in " ".join(bs._PAGE_SQL.split())
    find = " ".join(bs._FIND_SQL.split())
    assert "row_number() OVER (ORDER BY recorded_at DESC, box_number DESC NULLS LAST, box_code)" in find


def test_find_by_box_id_opens_its_page():
    conn = FakeConn(summary=_summary(25), found={"box_code": "9568129-1", "pos": 13})
    out = run(bs.list_boxes(conn, User(), 27385955, page=1, find=" 9568129-1 "))
    assert conn.ran("SELECT box_code, pos FROM")[0][2] == (27385955, "9568129-1", None)
    assert out["found"] == "9568129-1" and out["page"] == 2 and _page_args(conn) == (27385955, 10, 10)


def test_find_by_a_scanned_sticker_uses_its_box_id():
    conn = FakeConn(summary=_summary(3), found={"box_code": "9568129-1", "pos": 1})
    run(bs.list_boxes(conn, User(), 27385955, find='{"tx":"27385955","bi":"9568129-1"}'))
    assert conn.ran("SELECT box_code, pos FROM")[0][2] == (27385955, "9568129-1", None)


def test_find_by_box_number():
    conn = FakeConn(summary=_summary(25), found={"box_code": "9568414-3", "pos": 23})
    out = run(bs.list_boxes(conn, User(), 27385955, find="3"))
    assert conn.ran("SELECT box_code, pos FROM")[0][2] == (27385955, "3", 3)
    assert out["found"] == "9568414-3" and out["page"] == 3


def test_a_long_number_is_only_a_box_id():
    conn = FakeConn(summary=_summary(1))
    run(bs.list_boxes(conn, User(), 27385955, find="1234567890123"))
    assert conn.ran("SELECT box_code, pos FROM")[0][2] == (27385955, "1234567890123", None)


def test_find_that_misses_keeps_the_asked_page():
    conn = FakeConn(summary=_summary(25), found=None)
    out = run(bs.list_boxes(conn, User(), 27385955, page=2, find="NOPE-1"))
    assert out["found"] is None and out["page"] == 2 and _page_args(conn) == (27385955, 10, 10)


def test_a_blank_find_is_no_find():
    conn = FakeConn(summary=_summary(5))
    run(bs.list_boxes(conn, User(), 27385955, find="   "))
    assert not conn.ran("SELECT box_code, pos FROM")


@pytest.mark.parametrize("odd", ["\u00b2", "\u2460", "\u00b9\u00b2"])
def test_a_non_ascii_digit_is_only_a_box_id_and_misses_quietly(odd):
    conn = FakeConn(summary=_summary(5), found=None)
    out = run(bs.list_boxes(conn, User(), 27385955, find=odd))
    assert conn.ran("SELECT box_code, pos FROM")[0][2] == (27385955, odd, None)
    assert out["found"] is None


def test_a_nul_byte_in_find_is_a_miss_not_a_database_error():
    conn = FakeConn(summary=_summary(5))
    out = run(bs.list_boxes(conn, User(), 27385955, find="AB\x00C"))
    assert not conn.ran("SELECT box_code, pos FROM")
    assert out["found"] is None


def test_find_prefers_an_exact_box_id_over_a_box_number():
    find = " ".join(bs._FIND_SQL.split())
    assert "upper(box_code) = upper($2) OR ($3::int IS NOT NULL AND box_number = $3)" in find
    assert "ORDER BY (upper(box_code) = upper($2)) DESC, pos" in find


# ── print ──
def _line(n, net=9.77, gross=10.0, count=3, lot="L-7"):
    return {"box_number": n, "net_weight": net, "gross_weight": gross, "count": count, "lot_number": lot}


def test_print_mints_rm_boxes_in_sfg_box_and_records_them(monkeypatch):
    monkeypatch.setattr(bs, "new_short_time_id", lambda: 48213307)
    conn = FakeConn(jc={"job_card_number": "PLAN-1-L1-S1", "entity": "cfpl"}, last=4)
    out = run(bs.print_boxes(conn, User(), 27385955, article="  Sunflower Seeds Roasted ",
                             stock_type="Fresh Stock", boxes=[_line(1), _line(2, lot="  ")]))
    assert [a[0] for a in conn.sfg_inserts] == ["48213307-5", "48213307-6"]
    assert conn.sfg_inserts[0] == ("48213307-5", 75009889, "PLAN-1-L1-S1", SEEDS, "cfpl", "Mezzanine",
                                   "L-7", 9.77, 10.0, 3, "Store Sam")
    assert conn.sfg_inserts[1][6] is None                     # blank LOT → NULL
    sql = " ".join(bs._INSERT_SFG_SQL.split())
    assert "VALUES ($1, 'rm', $2, $3, $4, $4, $5, $6, 'Stores', $7, $8, $9, $10, 'PRINTED', $11)" in sql
    assert conn.box_inserts[0] == (27385955, "48213307-5", 1, "27385955", SEEDS, "Fresh Stock", "L-7",
                                   9.77, 10.0, 3, "Store Sam")
    assert [b["box_code"] for b in out["boxes"]] == ["48213307-5", "48213307-6"]
    assert out["boxes"][0]["source"] == "printed" and out["boxes"][0]["box_number"] == 1
    assert conn.ran("SELECT status FROM floor_requisition WHERE requisition_id = $1 FOR UPDATE")


def test_print_without_a_readable_job_card_still_prints(monkeypatch):
    monkeypatch.setattr(bs, "new_short_time_id", lambda: 11111111)
    conn = FakeConn(jc=None)
    run(bs.print_boxes(conn, User(), 27385955, article=SEEDS, stock_type="Fresh Stock", boxes=[_line(1)]))
    assert conn.sfg_inserts[0][:5] == ("11111111-1", 75009889, None, SEEDS, None)


@pytest.mark.parametrize("line, error", [
    (_line(1, net=0), "bad_box"),
    (_line(1, net=-2), "bad_box"),
    (_line(1, net=float("inf")), "bad_box"),
    (_line(1, net=5, gross=4.99), "bad_box"),
    (_line(1, count=-1), "bad_box"),
    (_line(0), "bad_box_number"),
])
def test_print_refuses_a_bad_box(line, error):
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(), User(), 27385955, article=SEEDS, stock_type="Fresh Stock", boxes=[line]))
    assert _refusal(e) == (400, error)


def test_a_bad_box_message_names_the_box():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(), User(), 27385955, article=SEEDS, stock_type="Fresh Stock",
                           boxes=[_line(1), _line(7, net=0)]))
    assert e.value.message == "Box 7: net wt must be more than 0."


@pytest.mark.parametrize("kw, error", [
    ({"article": "   "}, "article_required"),
    ({"article": "x" * 501}, "article_too_long"),
    ({"stock_type": "Rotten"}, "bad_stock_type"),
    ({"boxes": []}, "bad_box_count"),
    ({"boxes": [_line(n) for n in range(1, 502)]}, "bad_box_count"),
    ({"boxes": [_line(1), _line(1)]}, "duplicate_box_number"),
])
def test_print_refuses_a_bad_request(kw, error):
    args = {"article": SEEDS, "stock_type": "Fresh Stock", "boxes": [_line(1)], **kw}
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(), User(), 27385955, **args))
    assert _refusal(e) == (400, error)


def test_print_refuses_a_box_number_already_used_on_the_request():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(FakeConn(taken=[3]), User(), 27385955, article=SEEDS, stock_type="Fresh Stock",
                           boxes=[_line(3), _line(4)]))
    assert _refusal(e) == (409, "box_number_taken")
    assert e.value.details["box_numbers"] == [3]


def test_print_only_while_issued_under_the_lock():
    conn = FakeConn(locked_status="received")
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.print_boxes(conn, User(), 27385955, article=SEEDS, stock_type="Fresh Stock", boxes=[_line(1)]))
    assert _refusal(e) == (409, "not_issued")
    assert not conn.sfg_inserts


# ── remove ──
def test_remove_a_scanned_box_deletes_only_its_row():
    conn = FakeConn(source="scanned")
    out = run(bs.remove_box(conn, User(), 27385955, " BX-1 "))
    assert out == {"requisition_id": 27385955, "box_code": "BX-1", "removed": True}
    assert not conn.ran("DELETE FROM sfg_box")
    assert conn.ran("DELETE FROM floor_requisition_box")[0][2] == (27385955, "BX-1")


def test_remove_a_printed_box_deletes_its_sfg_box_row_too():
    conn = FakeConn(source="printed")
    run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    kind, sql, args = conn.ran("DELETE FROM sfg_box")[0]
    assert "item_type = 'rm' AND status = 'PRINTED'" in sql and args == ("48213307-5",)
    assert conn.ran("DELETE FROM floor_requisition_box")


def test_a_printed_box_the_floor_scanned_cannot_be_removed():
    conn = FakeConn(source="printed", in_jc=True)
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    assert _refusal(e) == (409, "box_in_use")
    assert not conn.ran("DELETE")


def test_a_printed_box_that_moved_past_printed_cannot_be_removed():
    conn = FakeConn(source="printed", sfg_delete="DELETE 0", sfg_still_there=True)
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    assert _refusal(e) == (409, "box_in_use")
    assert not conn.ran("DELETE FROM floor_requisition_box")


def test_without_jc_box_scan_a_printed_box_is_not_in_use():
    conn = FakeConn(source="printed", jc_table=False)
    run(bs.remove_box(conn, User(), 27385955, "48213307-5"))
    assert not conn.ran("SELECT 1 FROM jc_box_scan")
    assert conn.ran("DELETE FROM sfg_box")


def test_remove_a_box_not_on_the_request_is_404():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(FakeConn(source=None), User(), 27385955, "BX-404"))
    assert _refusal(e) == (404, "box_not_on_request")


def test_remove_only_while_issued():
    with pytest.raises(svc.RequisitionError) as e:
        run(bs.remove_box(FakeConn(req=_req(status="received")), User(), 27385955, "BX-1"))
    assert _refusal(e) == (409, "not_issued")
