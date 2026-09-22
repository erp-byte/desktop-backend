"""The job card's Raw Material scan reads Stores' record first.

floor_requisition_box (migration 113) says which request — and so which job card
— Stores sent a box for, and what Stores recorded for it. A box sent for another
job card is refused before anything is written; one sent for this job card is
recorded with Stores' figures; a box Stores never sent scans exactly as before.
The scan and the job card's list of scans carry Stores' reference (request and
box number) so the Raw Material tab can show where a box came from.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import box_scan_service as bss

JC = 75009889
SEEDS = "Sunflower Seeds Roasted"


def _stores(**over):
    row = {"requisition_id": 27385955, "box_table": "sfg_box", "transaction_no": "27385955",
           "article": SEEDS, "net_weight": Decimal("9.770"), "gross_weight": Decimal("10.000"), "count": 3,
           "source": "printed", "box_number": 4, "job_card_id": JC, "job_card_number": "PLAN-7-L1-S2"}
    row.update(over)
    return row


PRINTED_REF = {"requisition_id": 27385955, "box_number": 4, "source": "printed"}


def _sfg(**over):
    row = {"carton_id": "9568129-1", "fg_sku_name": SEEDS, "sfg_code": SEEDS, "net_weight": Decimal("9.500"),
           "gross_weight": Decimal("9.900"), "units": 2, "batch_id": None}
    row.update(over)
    return row


class FakeConn:
    def __init__(self, *, stores=None, frb_table=True, sfg=None, po=None, dup=False, scans=(), refs=()):
        self.stores, self.frb_table, self.sfg, self.po, self.dup = stores, frb_table, sfg, po, dup
        self.scans, self.refs = list(scans), list(refs)
        self.calls: list[tuple[str, str, tuple]] = []

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        return s

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        if s.startswith("SELECT 1 FROM job_card_v2"):
            return 1
        if s.startswith("SELECT to_regclass('floor_requisition_box')"):
            return self.frb_table
        if s.startswith("SELECT 1 FROM jc_box_scan"):
            return 1 if self.dup else None
        if s.startswith("SELECT COALESCE(MAX(CAST(split_part(carton_id"):
            return 0                                         # the list's next Box #
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        if s.startswith("SELECT b.requisition_id, b.box_table"):
            return self.stores
        if s.startswith("SELECT carton_id, fg_sku_name"):
            return self.sfg
        if s.startswith("SELECT b.box_id, b.transaction_no"):
            return self.po
        if s.startswith("INSERT INTO jc_box_scan (job_card_id, batch_id, sfg_box_id"):
            keys = ("job_card_id", "batch_id", "sfg_box_id", "article", "net_weight", "gross_weight",
                    "count", "scanned_by")
            return dict(zip(keys, args))
        if s.startswith("INSERT INTO jc_box_scan (job_card_id, batch_id, transaction_no"):
            keys = ("job_card_id", "batch_id", "transaction_no", "box_id", "article", "net_weight",
                    "gross_weight", "count", "scanned_by")
            return dict(zip(keys, args))
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if s.startswith("SELECT * FROM jc_box_scan_enriched"):
            return self.scans
        if s.startswith("SELECT b.box_code, b.requisition_id"):
            return self.refs
        if s.startswith("SELECT carton_id, batch_code FROM sfg_box"):
            return []                                        # none printed on the Raw Material tab
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    def read_the_stores_table(self):
        return any("FROM floor_requisition_box" in c[1] for c in self.calls)

    def ran(self, prefix):
        return [i for i, c in enumerate(self.calls) if c[1].startswith(prefix)]


def _no_identify(monkeypatch):
    async def fake(conn, value):
        fake.called = True
        return {"found": False, "box_id": value}
    fake.called = False
    monkeypatch.setattr(bss, "identify_box", fake)
    return fake


def scan(conn, **kw):
    return asyncio.run(bss.scan_box(conn, job_card_id=JC, code=kw.pop("code", "9568129-1"),
                                    scanned_by="Floor Fay", **kw))


def test_a_box_sent_for_another_job_card_is_refused_before_anything_else(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(stores=_stores(job_card_id=11111111, job_card_number="PLAN-9-L1-S1"), sfg=_sfg())
    out = scan(conn)
    assert out == {"error": "sent_for_other_job_card", "box_id": "9568129-1", "requisition_id": 27385955,
                   "job_card_id": 11111111, "job_card_number": "PLAN-9-L1-S1"}
    assert not conn.ran("INSERT")
    assert not conn.ran("SELECT 1 FROM jc_box_scan")        # before the duplicate guard
    assert not conn.ran("SELECT carton_id")                 # and before the box tables


def test_the_stores_record_is_read_by_the_box_id_alone(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(stores=None, sfg=_sfg())
    scan(conn)
    i = conn.ran("SELECT b.requisition_id, b.box_table")[0]
    kind, sql, args = conn.calls[i]
    assert args == ("9568129-1",) and "WHERE b.box_code = $1" in sql


def test_a_box_sent_for_this_job_card_is_recorded_with_stores_figures(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(stores=_stores(), sfg=_sfg())
    out = scan(conn)
    assert out["scanned"] is True
    assert out["scan"] == {"job_card_id": JC, "batch_id": None, "sfg_box_id": "9568129-1", "article": SEEDS,
                           "net_weight": Decimal("9.770"), "gross_weight": Decimal("10.000"), "count": 3,
                           "scanned_by": "Floor Fay", "stores": PRINTED_REF}


def test_the_scan_says_which_stores_request_sent_the_box(monkeypatch):
    _no_identify(monkeypatch)
    out = scan(FakeConn(stores=_stores(), sfg=_sfg()))
    assert out["scan"]["stores"] == {"requisition_id": 27385955, "box_number": 4, "source": "printed"}


def test_a_scanned_stores_box_has_no_box_number(monkeypatch):
    _no_identify(monkeypatch)
    out = scan(FakeConn(stores=_stores(source="scanned", box_number=None), sfg=_sfg()))
    assert out["scan"]["stores"] == {"requisition_id": 27385955, "box_number": None, "source": "scanned"}


def test_values_typed_on_the_job_card_still_win(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(stores=_stores(), sfg=_sfg())
    out = scan(conn, net_weight=8.0, article="Seeds (re-weighed)")
    assert out["scan"]["net_weight"] == 8.0 and out["scan"]["article"] == "Seeds (re-weighed)"
    assert out["scan"]["gross_weight"] == Decimal("10.000")


def test_blank_stores_figures_keep_the_box_tables_values(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(stores=_stores(net_weight=None, gross_weight=None, count=None), sfg=_sfg())
    out = scan(conn)
    assert (out["scan"]["net_weight"], out["scan"]["gross_weight"], out["scan"]["count"]) == \
        (Decimal("9.500"), Decimal("9.900"), 2)


def test_a_box_stores_never_sent_scans_as_before(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(stores=None, sfg=_sfg())
    out = scan(conn)
    assert out["scan"]["net_weight"] == Decimal("9.500") and out["scan"]["count"] == 2
    assert out["scan"]["stores"] is None


def test_without_the_stores_table_nothing_is_read_from_it(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(frb_table=False, sfg=_sfg())
    out = scan(conn)
    assert not conn.ran("SELECT b.requisition_id, b.box_table")
    assert not conn.read_the_stores_table()
    assert out["scan"]["net_weight"] == Decimal("9.500")
    assert out["scan"]["stores"] is None


def test_a_box_only_in_the_stores_record_is_still_recorded(monkeypatch):
    fake = _no_identify(monkeypatch)
    conn = FakeConn(stores=_stores(box_table="po_box", transaction_no="TR-1"), sfg=None, po=None)
    out = scan(conn, code="BX-1")
    assert out["scan"] == {"job_card_id": JC, "batch_id": None, "transaction_no": "TR-1", "box_id": "BX-1",
                           "article": SEEDS, "net_weight": Decimal("9.770"), "gross_weight": Decimal("10.000"),
                           "count": 3, "scanned_by": "Floor Fay", "stores": PRINTED_REF}
    assert fake.called is False                              # Stores already resolved it


def test_a_printed_box_only_in_the_stores_record_goes_to_the_sfg_side(monkeypatch):
    _no_identify(monkeypatch)
    conn = FakeConn(stores=_stores(), sfg=None, po=None)
    out = scan(conn)
    assert out["scan"]["sfg_box_id"] == "9568129-1"


# ── the list ──
def _scan_row(**over):
    row = {"job_card_id": JC, "box_id": None, "sfg_box_id": None, "transaction_no": None, "article": SEEDS,
           "net_weight": Decimal("9.770"), "gross_weight": Decimal("10.000"), "count": 3}
    row.update(over)
    return row


def _ref(code, **over):
    row = {"box_code": code, "requisition_id": 27385955, "box_number": None, "source": "scanned"}
    row.update(over)
    return row


SCANS = [_scan_row(sfg_box_id="9568129-1"),
         _scan_row(box_id="BX-1", transaction_no="TR-1", net_weight=Decimal("10.500"),
                   gross_weight=Decimal("11.000"), count=4),
         _scan_row(box_id="BX-9", net_weight=Decimal("1.250"), gross_weight=None, count=None)]
TOTALS = {"boxes": 3, "net_weight": 21.52, "gross_weight": 21.0, "count": 7}


def listed(conn):
    return asyncio.run(bss.list_scans(conn, job_card_id=JC))


def test_the_list_says_which_stores_request_sent_each_box():
    conn = FakeConn(scans=SCANS, refs=[_ref("BX-1"), _ref("9568129-1", source="printed", box_number=4)])
    out = listed(conn)
    assert [s["stores"] for s in out["scans"]] == [
        PRINTED_REF,                                                              # by sfg_box_id
        {"requisition_id": 27385955, "box_number": None, "source": "scanned"},    # by box_id
        None,                                                                     # Stores never sent it
    ]


def test_the_list_asks_stores_once_for_this_job_card_and_its_boxes():
    conn = FakeConn(scans=SCANS)
    listed(conn)
    [i] = conn.ran("SELECT b.box_code, b.requisition_id")
    kind, sql, args = conn.calls[i]
    assert "WHERE fr.job_card_id = $1" in sql and "b.box_code = ANY($2::text[])" in sql
    assert args == (JC, ["9568129-1", "BX-1", "BX-9"])


def test_an_empty_list_asks_stores_nothing():
    conn = FakeConn(scans=[])
    out = listed(conn)
    # Just the job card's scans and its next Box #.
    assert out["scans"] == [] and [c[1][:30] for c in conn.calls] == [
        "SELECT * FROM jc_box_scan_enri", "SELECT COALESCE(MAX(CAST(split"]
    assert not conn.read_the_stores_table()


def test_without_the_stores_table_the_list_reads_nothing_from_it():
    conn = FakeConn(scans=SCANS, frb_table=False, refs=[_ref("BX-1")])
    out = listed(conn)
    assert not conn.ran("SELECT b.box_code") and not conn.read_the_stores_table()
    assert [s["stores"] for s in out["scans"]] == [None, None, None]


def test_stores_references_leave_the_totals_alone():
    with_refs = listed(FakeConn(scans=SCANS, refs=[_ref("BX-1"), _ref("9568129-1")]))
    without = listed(FakeConn(scans=SCANS, frb_table=False))
    assert with_refs["totals"] == without["totals"] == TOTALS
    assert with_refs["job_card_id"] == JC
    assert set(with_refs) == {"job_card_id", "scans", "totals", "next_box_number"}


# ── the route ──
class _Ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def test_the_route_turns_the_refusal_into_a_422_with_the_job_card(monkeypatch):
    async def refuse(conn, **kw):
        return {"error": "sent_for_other_job_card", "box_id": "9568129-1", "requisition_id": 27385955,
                "job_card_id": 11111111, "job_card_number": "PLAN-9-L1-S1"}

    monkeypatch.setattr(bss, "scan_box", refuse)
    pool = SimpleNamespace(acquire=lambda: _Ctx(object()))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    user = SimpleNamespace(full_name="Floor Fay", phone=None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(PR.create_box_scan(request, JC, PR.BoxScanRequest(code="9568129-1"), user=user))
    # 422, not 409: the Raw Material tab shows every 409 as the "Duplicate box" chip.
    assert e.value.status_code == 422
    assert e.value.detail == ("Box 9568129-1 was sent by Stores for job card PLAN-9-L1-S1 "
                              "(request #27385955). Scan it on that job card.")


def test_an_unknown_box_without_an_article_is_a_422_with_a_code(monkeypatch):
    # The Raw Material tab has no article field (Manual print replaces it), so it
    # reads the code to tell the operator to print a sticker instead.
    async def unknown(conn, **kw):
        return {"error": "article_required", "code": "ZZ-1"}

    monkeypatch.setattr(bss, "scan_box", unknown)
    pool = SimpleNamespace(acquire=lambda: _Ctx(object()))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    user = SimpleNamespace(full_name="Floor Fay", phone=None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(PR.create_box_scan(request, JC, PR.BoxScanRequest(code="ZZ-1"), user=user))
    assert e.value.status_code == 422
    assert e.value.detail == {"error": "article_required",
                              "message": "'ZZ-1' isn't in any catalogue — enter an article to store it."}


def test_the_route_hands_back_the_scan_with_stores_reference(monkeypatch):
    async def ok(conn, **kw):
        return {"scanned": True, "scan": {"sfg_box_id": "9568129-1", "stores": PRINTED_REF}}

    monkeypatch.setattr(bss, "scan_box", ok)
    pool = SimpleNamespace(acquire=lambda: _Ctx(object()))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    user = SimpleNamespace(full_name="Floor Fay", phone=None)
    out = asyncio.run(PR.create_box_scan(request, JC, PR.BoxScanRequest(code="9568129-1"), user=user))
    assert out == {"scanned": True, "scan": {"sfg_box_id": "9568129-1", "stores": PRINTED_REF}}
