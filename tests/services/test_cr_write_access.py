"""Customer-return writes: who may write (X1) and what a decided return still allows (X2).

X1  Every write route needs its customer_returns permission, checked on the server:
    admins pass on their flag, anyone else needs the grant, and an account locked to
    companies writes only to those. Reads and the decision itself stay as they were
    (a business head reviews and decides without being an admin).
X2  A return's header and lines change only while it is undecided (Pending / On
    Hold). Its boxes stay open after approval (the "save boxes after approval" step)
    but close once it is rejected.

No database: the HTTP half runs the real router over test_cr_approval_decisions'
fakes; the service half answers each statement from a small in-memory header and
stops at the first write it reaches (_Reached), which proves the gate let it through.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_write_access.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import asyncpg
import pytest
from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import (box_service, cold_stock_service, create_service,
                                                   query_service, wa_notify)
from tests.services.test_cr_approval_decisions import (  # noqa: F401  (fixtures)
    ADMIN, BH, COMPANY, CR, CREATOR, FakeConn, client, get_cr, header, mail, no_guard_errors, tokens,
)

BASE = f"/api/v1/customer-returns/{COMPANY}"
LINE = {"material_type": "RM", "item_category": "N", "sub_category": "S",
        "item_description": "ALMOND W-320", "uom": "10", "qty": "2", "rate": "5"}
BOX = {"article_description": "ALMOND W-320", "box_number": 1, "net_weight": "10.000"}

# (method, path, body, permission the route checks)
WRITES = [
    ("POST", BASE, {"company": COMPANY, "header": {"factory_unit": "W202", "customer": "ACME"},
                    "lines": [LINE]}, ("returns", "create")),
    ("PUT", f"{BASE}/{CR}", {"remark": "x"}, ("returns", "edit")),
    ("PUT", f"{BASE}/{CR}/lines", {"lines": [LINE]}, ("returns", "edit")),
    ("DELETE", f"{BASE}/{CR}", None, ("returns", "delete")),
    ("PUT", f"{BASE}/{CR}/box", BOX, ("boxes", "edit")),
    ("PUT", f"{BASE}/{CR}/boxes", {"boxes": [BOX]}, ("boxes", "edit")),
    ("POST", "/api/v1/customer-returns/box-edit-log",
     {"email_id": "x", "box_id": "12345678-1", "rtv_id": CR,
      "changes": [{"field_name": "lot_number", "old_value": "A", "new_value": "B"}]}, ("boxes", "edit")),
    ("POST", f"{BASE}/{CR}/send-for-approval", None, ("returns", "create")),
]
_IDS = [f"{m} {p.rsplit('customer-returns', 1)[1]}" for m, p, _, _ in WRITES]


def _call(c, method, path, body):
    return c.request(method, path, json=body) if body is not None else c.request(method, path)


def _writes(conn):
    return [k for k in conn.calls if re.match(r"(INSERT|UPDATE|DELETE)\b", k[1])]


# ── X1: who may write ────────────────────────────────────────────────────────
@pytest.mark.parametrize("method, path, body, perm", WRITES, ids=_IDS)
def test_a_user_without_the_permission_is_refused_and_nothing_is_written(client, method, path, body, perm):
    conn = FakeConn(header())
    res = _call(client(conn, CREATOR, allowed=False), method, path, body)
    assert res.status_code == 403, res.text
    assert res.json()["error"] == "forbidden"
    assert res.json()["details"]["permission"] == f"customer_returns.{perm[0]}.{perm[1]}"
    # Refused before the route touched the return: the gate only read its unit.
    assert all(c[1].startswith("SELECT factory_unit FROM") for c in conn.calls), conn.calls
    entity, unit = (None, None) if "box-edit-log" in path else ("cfpl", "W202")
    assert client.asked == [("customer_returns", perm[0], perm[1], entity, unit)]


@pytest.mark.parametrize("method, path, body, perm", WRITES, ids=_IDS)
def test_an_admin_is_not_asked_for_a_permission(client, mail, monkeypatch, method, path, body, perm):
    monkeypatch.setattr(wa_notify, "_wa_enabled", lambda: False)
    c = client(FakeConn(header()), ADMIN, allowed=False)
    try:
        res = _call(c, method, path, body)
        assert res.status_code != 403, res.text
    except AssertionError as exc:
        # The fake answers only the decision tests' SQL, so a route that got past the
        # gate may stop on its own first statement; only the gate matters here.
        assert "unexpected" in str(exc), exc
    assert client.asked == []


def test_an_account_locked_to_the_other_company_is_refused_before_any_grant_is_read(client):
    conn = FakeConn(header())
    res = client(conn, CREATOR, allowed=True, allowed_entities=["cdpl"]).put(f"{BASE}/{CR}", json={"remark": "x"})
    assert res.status_code == 403
    assert res.json()["message"] == "You can't edit customer returns for CFPL."
    assert client.asked == [] and conn.calls == []


@pytest.mark.parametrize("method, path, body, unit", [
    ("PUT", f"{BASE}/{CR}", {"remark": "x"}, "W202"),                        # the return's own unit
    ("PUT", f"{BASE}/{CR}/boxes", {"boxes": [BOX]}, "W202"),
    ("DELETE", f"{BASE}/{CR}", None, "W202"),
    ("POST", BASE, {"company": COMPANY, "header": {"factory_unit": "W202", "customer": "ACME"},
                    "lines": [LINE]}, "W202"),                                # the unit a create names
])
def test_an_account_locked_to_another_warehouse_is_refused(client, method, path, body, unit):
    conn = FakeConn(header())
    res = _call(client(conn, CREATOR, allowed=True, allowed_warehouses=["A185"]), method, path, body)
    assert res.status_code == 403, res.text
    assert res.json()["details"]["factory_unit"] == unit
    assert client.asked == [] and not _writes(conn)


def test_a_return_cannot_be_moved_to_a_warehouse_outside_the_lock(client):
    conn = FakeConn(header())                      # a W202 return, edited by a W202-locked user
    res = client(conn, CREATOR, allowed=True, allowed_warehouses=["W-202"]).put(
        f"{BASE}/{CR}", json={"factory_unit": "A185"})
    assert res.status_code == 403 and res.json()["details"]["factory_unit"] == "A185"
    assert not _writes(conn) and conn.headers[CR]["factory_unit"] == "W202"


def test_a_warehouse_lock_matches_the_unit_however_it_is_spelt(client):
    conn = FakeConn(header())
    res = client(conn, CREATOR, allowed=True, allowed_warehouses=["W-202"]).put(f"{BASE}/{CR}", json={"remark": "x"})
    assert res.status_code == 200, res.text
    assert client.asked == [("customer_returns", "returns", "edit", "cfpl", "W202")]


def test_a_granted_user_locked_to_this_company_gets_through(client):
    conn = FakeConn(header())
    res = client(conn, CREATOR, allowed=True, allowed_entities=["CFPL"]).put(f"{BASE}/{CR}", json={"remark": "x"})
    assert res.status_code == 200, res.text
    assert res.json()["remark"] == "x"


def test_reading_a_return_needs_no_customer_returns_permission(client, monkeypatch):
    async def _get_cr(conn, company, cr_id):   # the route got past auth to its read
        raise HTTPException(418, detail={"error": "read_reached", "details": {"rtv_id": cr_id}})

    monkeypatch.setattr(query_service, "get_cr", _get_cr)
    res = client(FakeConn(header()), BH, allowed=False).get(f"{BASE}/{CR}")
    assert res.status_code == 418 and res.json()["error"] == "read_reached"
    assert client.asked == []


def test_the_decision_route_asks_for_no_customer_returns_permission(client, mail, get_cr):
    # A business head decides without being granted anything; approval_service's own
    # rule decides who may (here: the BH may hold a Pending return).
    conn = FakeConn(header())
    res = client(conn, BH, allowed=False).post(f"{BASE}/{CR}/approve", json={"action": "hold"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "On Hold"
    assert client.asked == []


# ── X2: what a decided return still allows (service level) ───────────────────
class _Reached(Exception):
    """The call got past the gate to its first real write."""


class Conn:
    """One cfpl_rtv_header row and its boxes; answers the gates' reads and stops at the
    first write (stop=False records the writes instead; log_missing makes
    cr_deletion_log absent). box_rows: the stored boxes, as (article, number, box_id);
    version: the box-set fingerprint; ims_rows: cold-stock rows IMS made from it (None:
    no cold table on this database)."""

    def __init__(self, status, factory_unit="W202", *, boxes=0, stop=True, log_missing=False,
                 box_rows=(), version="v1", ims_rows=0):
        self.row = {"id": 501, "rtv_id": CR, "status": status, "factory_unit": factory_unit,
                    "customer": "ACME FOODS", "invoice_number": "INV-9", "challan_no": "CH-1",
                    "business_head": "Prashant Pal", "created_by": "w@x.in"}
        self.box_rows = [{"id": 900 + i, "article_description": a, "box_number": n, "box_id": bid,
                          **{f: None for f in box_service._TRACKED}}
                         for i, (a, n, bid) in enumerate(box_rows)]
        self.boxes = boxes or len(self.box_rows)
        self.stop = stop
        self.log_missing = log_missing
        self.version = version
        self.ims_rows = ims_rows
        self.vanished = False          # the header is deleted between the box route's checks
        self.calls: list[str] = []
        self.recorded: list[tuple[str, tuple]] = []     # (statement, its arguments)

    def last_args(self, statement: str) -> tuple:
        return next(a for s, a in reversed(self.recorded) if s == statement)

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    def _log(self, sql, args=()):
        s = " ".join(sql.split())
        self.calls.append(s)
        self.recorded.append((s, args))
        if self.log_missing and s.startswith("INSERT INTO cr_deletion_log"):
            raise asyncpg.UndefinedTableError('relation "cr_deletion_log" does not exist')
        if self.stop and re.match(r"(INSERT|UPDATE|DELETE)\b", s):
            raise _Reached(s)
        return s

    async def fetchrow(self, sql, *args):
        s = self._log(sql, args)
        m = re.fullmatch(r"SELECT ([\w, ]+) FROM cfpl_rtv_header WHERE rtv_id = \$1(?: FOR UPDATE)?", s)
        if m:
            return {c.strip(): self.row[c.strip()] for c in m[1].split(",")} if args[0] == self.row["rtv_id"] else None
        if s.startswith("SELECT id, article_description, box_number, box_id,") and "box_number = $3" in s:
            hit = [b for b in self.box_rows if (b["article_description"], b["box_number"]) == (args[1], args[2])]
            return dict(hit[0]) if hit else None
        if s == "SELECT status FROM cfpl_rtv_header WHERE id = $1 FOR SHARE":
            return None if self.vanished else {"status": self.row["status"]}
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def fetchval(self, sql, *args):
        s = self._log(sql, args)
        if s.startswith("SELECT id FROM cfpl_rtv_lines WHERE header_id = $1 AND item_description = $2"):
            return 9
        if s == "SELECT COUNT(*) FROM cfpl_rtv_boxes WHERE header_id = $1":
            return self.boxes
        if s.startswith("SELECT md5(") and "FROM cfpl_rtv_boxes WHERE header_id = $1" in s:
            return self.version
        if s == "SELECT to_regclass($1::text) IS NOT NULL":
            return self.ims_rows is not None
        if s.startswith("SELECT COUNT(*) FROM cfpl_cold_stocks WHERE inward_transaction_no = $1"):
            return self.ims_rows
        if s.startswith("SELECT EXISTS (SELECT 1 FROM cfpl_rtv_lines WHERE header_id = $1)"):
            return False                     # box_service._set_boxed_status: not fully boxed
        if s == "SELECT to_regclass('public.box_edit_logs') IS NOT NULL":
            return True
        raise AssertionError(f"unexpected fetchval: {s}")

    async def fetch(self, sql, *args):
        s = self._log(sql, args)
        if s.startswith("SELECT id, article_description, box_number, box_id,") and "box_number = $3" not in s:
            return [dict(b) for b in self.box_rows]
        return []

    async def execute(self, sql, *args):
        self._log(sql, args)
        return "OK"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _label_ids_are_free(monkeypatch):
    """box_service mints a label id no box table holds: here every id is free."""
    async def free(conn, box_id):
        return False
    monkeypatch.setattr(box_service.box_identify_service, "box_id_taken", free)


def _refused(coro) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        run(coro)
    return exc.value


DECIDED = [("Approved", "Approved"), ("Submitted", "Approved (boxes saved)"), ("Rejected", "Rejected"),
           (" Approved ", "Approved")]
UNDECIDED = ["Pending", "On Hold", None, "", "  "]


def _header_edit(conn):
    return create_service.update_cr(conn, COMPANY, CR, schemas.CRHeaderUpdate(remark="x"))


def _lines_edit(conn):
    return create_service.update_cr_lines(conn, COMPANY, CR, schemas.CRLinesUpdateRequest(
        lines=[schemas.CRLineCreate(**LINE)]))


@pytest.mark.parametrize("edit", [_header_edit, _lines_edit], ids=["header", "lines"])
@pytest.mark.parametrize("status, label", DECIDED)
def test_a_decided_return_s_header_and_lines_cannot_change(edit, status, label):
    conn = Conn(status)
    exc = _refused(edit(conn))
    assert exc.status_code == 409 and exc.detail["error"] == "already_decided"
    assert exc.detail["message"] == f"{CR} is already “{label}”, so its details and lines can't be changed."
    assert not any(re.match(r"(INSERT|UPDATE|DELETE)\b", s) for s in conn.calls)
    # The check holds the row: a decision landing now waits for it.
    assert conn.calls[0] == "SELECT id, status, factory_unit FROM cfpl_rtv_header WHERE rtv_id = $1 FOR UPDATE"


@pytest.mark.parametrize("edit, first_write", [
    (_header_edit, "UPDATE cfpl_rtv_header SET remark = $1, updated_at = NOW() WHERE id = $2 RETURNING "),
    (_lines_edit, "DELETE FROM cfpl_rtv_lines WHERE header_id = $1"),
], ids=["header", "lines"])
@pytest.mark.parametrize("status", UNDECIDED)
def test_an_undecided_return_s_header_and_lines_still_change(edit, first_write, status):
    conn = Conn(status)
    with pytest.raises(_Reached) as reached:
        run(edit(conn))
    assert str(reached.value).startswith(first_write)


@pytest.mark.parametrize("edit", [_header_edit, _lines_edit], ids=["header", "lines"])
def test_editing_an_unknown_return_is_404(edit):
    conn = Conn("Pending")
    conn.row["rtv_id"] = "CR-OTHER"
    exc = _refused(edit(conn))
    assert exc.status_code == 404 and exc.detail["error"] == "customer_return_not_found"


def _print(conn):
    return box_service.upsert_box(conn, COMPANY, CR, schemas.CRBoxUpsertRequest(**BOX))


def _sync(conn):
    return box_service.bulk_save_boxes(conn, COMPANY, CR, schemas.CRBulkBoxUpdateRequest(boxes=[BOX]))


@pytest.mark.parametrize("write", [_print, _sync], ids=["print", "save"])
@pytest.mark.parametrize("status", ["Rejected", " Rejected"])
def test_a_rejected_return_s_boxes_are_closed(write, status):
    conn = Conn(status)
    exc = _refused(write(conn))
    assert exc.status_code == 409 and exc.detail["error"] == "rejected_return"
    assert exc.detail["message"] == f"{CR} was rejected, so its boxes can't be changed."
    assert not any(re.match(r"(INSERT|UPDATE|DELETE)\b", s) for s in conn.calls)
    # Read under the CR's box lock and FOR SHARE, so a rejection can't slip in after it.
    lock = next(i for i, s in enumerate(conn.calls) if "pg_advisory_xact_lock" in s)
    assert conn.calls[lock + 1] == "SELECT status FROM cfpl_rtv_header WHERE id = $1 FOR SHARE"


@pytest.mark.parametrize("write", [_print, _sync], ids=["print", "save"])
@pytest.mark.parametrize("status", ["Pending", "On Hold", None, "Approved", "Submitted"])
def test_boxes_stay_open_before_the_decision_and_after_approval(write, status):
    with pytest.raises(_Reached) as reached:
        run(write(Conn(status)))
    assert str(reached.value).startswith("INSERT INTO cfpl_rtv_boxes")


@pytest.mark.parametrize("write", [_print, _sync], ids=["print", "save"])
def test_a_return_deleted_mid_write_is_404_not_a_failed_insert(write):
    conn = Conn("Pending")
    conn.vanished = True
    exc = _refused(write(conn))
    assert exc.status_code == 404 and exc.detail["error"] == "customer_return_not_found"
    assert not any(re.match(r"(INSERT|UPDATE|DELETE)", s) for s in conn.calls)


# ── B4: cold-unit returns ────────────────────────────────────────────────────
@pytest.mark.parametrize("ims_rows", [0, None], ids=["no IMS copy", "no cold table"])
@pytest.mark.parametrize("write", [_print, _sync], ids=["print", "save"])
def test_a_cold_return_s_boxes_are_entered_here_now(write, ims_rows):
    with pytest.raises(_Reached) as reached:
        run(write(Conn("Pending", factory_unit="Savla D-39", ims_rows=ims_rows)))
    assert str(reached.value).startswith("INSERT INTO cfpl_rtv_boxes")


@pytest.mark.parametrize("write", [_print, _sync], ids=["print", "save"])
def test_a_cold_return_ims_copied_into_cold_stock_stays_as_it_is(write):
    conn = Conn("Approved", factory_unit="D-39", ims_rows=4)
    exc = _refused(write(conn))
    assert exc.status_code == 409 and exc.detail["error"] == "cold_stock_from_ims"
    assert "IMS" in exc.detail["message"] and exc.detail["details"]["cold_stock_rows"] == 4
    assert not any(re.match(r"(INSERT|UPDATE|DELETE)\b", s) for s in conn.calls)


def test_a_warehouse_return_never_looks_at_cold_stock():
    conn = Conn("Pending", factory_unit="W202", ims_rows=4)
    with pytest.raises(_Reached):
        run(_print(conn))
    assert not any("cold_stocks" in s or "to_regclass" in s for s in conn.calls)


@pytest.fixture
def cold_syncs(monkeypatch):
    """cold_stock_service.sync, recorded with the statement count at the call."""
    seen = []

    async def _sync(conn, company, cr_id):
        seen.append((company, cr_id, len(conn.calls)))
        return {"added": 0, "corrected": 0}

    monkeypatch.setattr(cold_stock_service, "sync", _sync)
    return seen


@pytest.mark.parametrize("write", [_print, _sync], ids=["print", "save"])
@pytest.mark.parametrize("status", ["Approved", "Submitted", " Approved "])
def test_a_box_write_on_an_approved_cold_return_updates_its_cold_stock(write, status, cold_syncs):
    conn = Conn(status, factory_unit="Rishi Cold", stop=False)
    run(write(conn))
    ((company, cr_id, at),) = cold_syncs
    assert (company, cr_id) == (COMPANY, CR)
    # after the box write, inside the same transaction (before the new fingerprint)
    box_write = max(i for i, s in enumerate(conn.calls) if re.match(r"(INSERT INTO|UPDATE) cfpl_rtv_boxes\b", s))
    assert box_write < at and conn.calls[at].startswith("SELECT md5(")


@pytest.mark.parametrize("write", [_print, _sync], ids=["print", "save"])
@pytest.mark.parametrize("status, unit", [("Pending", "Savla D-39"), ("On Hold", "Savla D-39"),
                                          ("Approved", "W202"), ("Approved", "Cold Storage")])
def test_other_box_writes_leave_cold_stock_alone(write, status, unit, cold_syncs):
    run(write(Conn(status, factory_unit=unit, stop=False)))
    assert cold_syncs == []


# ── B1 · B2 · B3: printed boxes ──────────────────────────────────────────────
ART = BOX["article_description"]
PRINTED = (ART, 1, "12345678-1")
UNPRINTED = (ART, 2, None)


def _save(conn, boxes, base=None):
    return run(box_service.bulk_save_boxes(conn, COMPANY, CR, schemas.CRBulkBoxUpdateRequest(
        boxes=[schemas.CRBulkBoxItem(**b) for b in boxes], base_version=base), allow_clear=True))


def _writes_in(conn):
    return [s for s in conn.calls if re.match(r"(INSERT|UPDATE|DELETE)\b", s)]


def test_a_save_never_deletes_a_printed_box_left_out_of_it():
    # B1 / B3: a page that doesn't know a printed box (cancelled edit, stale page) saves.
    conn = Conn("Pending", stop=False, box_rows=[PRINTED, UNPRINTED])
    out = _save(conn, [])
    deletes = [s for s in conn.calls if s.startswith("DELETE FROM cfpl_rtv_boxes")]
    assert len(deletes) == 1                      # the unprinted box only
    assert (out["deleted"], out["kept_printed"]) == (1, 1)
    assert out["box_version"] == "v1"


def test_a_save_that_thinks_a_printed_box_is_unprinted_is_refused():
    # B2: after a cancelled edit the page offers box 1 as new; the server has it printed.
    conn = Conn("Pending", stop=False, box_rows=[PRINTED])
    with pytest.raises(HTTPException) as exc:
        _save(conn, [{"article_description": ART, "box_number": 1, "net_weight": "9"}])
    assert exc.value.status_code == 409 and exc.value.detail["error"] == "box_changed"
    assert "already has printed label 12345678-1" in exc.value.detail["message"]
    assert _writes_in(conn) == []


def test_a_save_naming_the_right_label_updates_the_printed_box():
    conn = Conn("Approved", stop=False, box_rows=[PRINTED])
    out = _save(conn, [{"article_description": ART, "box_number": 1, "box_id": "12345678-1", "net_weight": "9"}])
    assert out["updated"] == 1 and _writes_in(conn)[0].startswith("UPDATE cfpl_rtv_boxes SET")


@pytest.mark.parametrize("rows, number, sent", [([UNPRINTED], 2, "12345678-2"), ([], 3, "12345678-3")],
                         ids=["stored unprinted", "no longer there"])
def test_a_save_naming_a_label_the_server_doesnt_have_is_refused(rows, number, sent):
    conn = Conn("Pending", stop=False, box_rows=rows)
    with pytest.raises(HTTPException) as exc:
        _save(conn, [{"article_description": ART, "box_number": number, "box_id": sent}])
    assert exc.value.detail["error"] == "box_changed" and _writes_in(conn) == []


def test_one_mismatch_refuses_the_whole_save_before_anything_is_written():
    conn = Conn("Pending", stop=False, box_rows=[PRINTED, UNPRINTED])
    with pytest.raises(HTTPException):
        _save(conn, [{"article_description": ART, "box_number": 2, "net_weight": "5"},
                     {"article_description": ART, "box_number": 1, "net_weight": "9"}])
    assert _writes_in(conn) == []


def test_printing_a_number_that_was_printed_elsewhere_is_refused():
    # B2: print, Cancel, Edit, Add Box reuses number 1, Print.
    conn = Conn("Pending", stop=False, box_rows=[PRINTED])
    exc = _refused(_print(conn))                  # BOX is number 1, sent with no label
    assert exc.status_code == 409 and exc.detail["error"] == "box_changed"
    assert _writes_in(conn) == []


def test_reprinting_a_box_with_its_own_label_keeps_the_label():
    conn = Conn("Pending", stop=False, box_rows=[PRINTED])
    out = run(box_service.upsert_box(conn, COMPANY, CR, schemas.CRBoxUpsertRequest(**BOX, box_id="12345678-1")))
    assert out["box_id"] == "12345678-1" and out["status"] == "updated" and out["box_version"] == "v1"


@pytest.mark.parametrize("write", ["print", "save"])
def test_a_write_from_an_out_of_date_page_is_refused(write):
    # B3: the boxes changed since this page loaded them (fingerprint v1 -> v2).
    conn = Conn("Pending", stop=False, box_rows=[PRINTED], version="v2")
    with pytest.raises(HTTPException) as exc:
        if write == "print":
            run(box_service.upsert_box(conn, COMPANY, CR, schemas.CRBoxUpsertRequest(
                article_description=ART, box_number=5, base_version="v1")))
        else:
            _save(conn, [{"article_description": ART, "box_number": 1, "box_id": "12345678-1"}], base="v1")
    assert exc.value.status_code == 409 and exc.value.detail["error"] == "boxes_changed"
    assert _writes_in(conn) == []


def test_a_write_from_an_up_to_date_page_goes_through_and_returns_the_new_fingerprint():
    conn = Conn("Pending", stop=False, box_rows=[PRINTED], version="v1")
    out = run(box_service.upsert_box(conn, COMPANY, CR, schemas.CRBoxUpsertRequest(
        article_description=ART, box_number=5, base_version="v1")))
    assert out["status"] == "inserted" and out["box_version"] == "v1"


# ── C1: deleting a return ────────────────────────────────────────────────────
DETAIL = {"rtv_id": CR, "status": "Pending", "customer": "ACME", "factory_unit": "W202",
          "lines": [{"item_description": "ALMOND W-320"}],
          "boxes": [{"box_number": 1, "box_id": "12345678-1"}, {"box_number": 2, "box_id": None}]}


@pytest.fixture
def snapshot(monkeypatch):
    async def _get_cr(conn, company, cr_id):
        return {**DETAIL, "status": conn.row["status"]}

    monkeypatch.setattr(query_service, "get_cr", _get_cr)


def _delete(conn):
    return create_service.delete_cr(conn, COMPANY, CR, deleted_by="admin@candorfoods.in")


@pytest.mark.parametrize("status, label", DECIDED)
def test_a_decided_return_cannot_be_deleted(snapshot, status, label):
    conn = Conn(status, stop=False)
    exc = _refused(_delete(conn))
    assert exc.status_code == 409 and exc.detail["error"] == "already_decided"
    assert exc.detail["message"] == f"{CR} is already “{label}”, so it can't be deleted."
    assert not any(re.match(r"(INSERT|UPDATE|DELETE)\b", s) for s in conn.calls)


@pytest.mark.parametrize("status", UNDECIDED)
def test_an_undecided_return_is_recorded_then_deleted(snapshot, status):
    conn = Conn(status, stop=False)
    out = run(_delete(conn))
    writes = [s for s in conn.calls if re.match(r"(INSERT|DELETE)\b", s)]
    assert writes[0].startswith("INSERT INTO cr_deletion_log")
    assert writes[1] == "DELETE FROM cfpl_rtv_header WHERE rtv_id = $1"
    assert conn.calls[0].endswith("FOR UPDATE")      # held from the check to the delete
    assert (out["success"], out["lines_count"], out["boxes_count"], out["printed_boxes"]) == (True, 1, 2, 1)
    assert out["detail"]["rtv_id"] == CR


def test_the_record_holds_who_deleted_it_and_what_it_was(snapshot):
    class Recording(Conn):
        async def execute(self, sql, *args):
            s = self._log(sql)
            if s.startswith("INSERT INTO cr_deletion_log"):
                self.record = args
            return "OK"

    conn = Recording("On Hold", stop=False)
    run(_delete(conn))
    company, rtv_id, status, by, lines, boxes, printed, snap = conn.record
    assert (company, rtv_id, status, by, lines, boxes, printed) == \
        ("CFPL", CR, "On Hold", "admin@candorfoods.in", 1, 2, 1)
    assert json.loads(snap)["boxes"][0]["box_id"] == "12345678-1"


def test_without_the_log_table_the_delete_still_happens_and_the_record_goes_to_the_log(snapshot, caplog):
    caplog.set_level(logging.WARNING)
    conn = Conn("Pending", stop=False, log_missing=True)
    out = run(_delete(conn))
    assert out["success"] and "DELETE FROM cfpl_rtv_header WHERE rtv_id = $1" in conn.calls
    assert any("cr_deletion_log missing" in r.getMessage() and CR in r.getMessage() for r in caplog.records)


def test_deleting_tells_the_mail_thread_who_deleted_it(client, mail, get_cr):
    conn = FakeConn(header())
    res = client(conn, ADMIN).delete(f"{BASE}/{CR}")
    assert res.status_code == 200, res.text
    assert res.json()["rtv_id"] == CR and "detail" not in res.json()
    assert CR not in conn.headers and len(conn.deletion_log) == 1
    (notice,) = mail.sent
    assert f"deleted by {ADMIN.email}" in notice["text"]
    assert notice["irt"] == f"<RTV-{CR}@candorfoods.in>"


def test_discarding_a_draft_sends_no_mail(client, mail, get_cr):
    conn = FakeConn(header())
    res = client(conn, ADMIN).delete(f"{BASE}/{CR}?notify=false")
    assert res.status_code == 200 and CR not in conn.headers
    assert mail.sent == [] and len(conn.deletion_log) == 1


def test_deleting_an_approved_return_through_the_api_is_refused(client, mail, get_cr):
    conn = FakeConn(header(status="Approved", approved_by="satyendra@candorfoods.in"))
    res = client(conn, ADMIN).delete(f"{BASE}/{CR}")
    assert res.status_code == 409 and res.json()["error"] == "already_decided"
    assert CR in conn.headers and conn.deletion_log == [] and mail.sent == []


# ── C2: correcting a return's details ────────────────────────────────────────
def _move_to(unit):
    return lambda conn: create_service.update_cr(conn, COMPANY, CR, schemas.CRHeaderUpdate(factory_unit=unit))


@pytest.mark.parametrize("old, new", [("W202", "Savla D-39"), ("Savla D-39", "W202")])
def test_a_return_with_boxes_cannot_cross_between_cold_store_and_warehouse(old, new):
    conn = Conn("Pending", factory_unit=old, boxes=3)
    exc = _refused(_move_to(new)(conn))
    assert exc.status_code == 409 and exc.detail["error"] == "factory_unit_locked"
    assert not any(re.match(r"UPDATE\b", s) for s in conn.calls)


@pytest.mark.parametrize("old, new, boxes", [
    ("W202", "A185", 3),              # warehouse to warehouse: a plain correction
    ("Savla D-39", "Rishi", 3),       # cold store to cold store
    ("W202", "Savla D-39", 0),        # no boxes yet: nothing to slip past
])
def test_other_factory_unit_corrections_go_through(old, new, boxes):
    conn = Conn("Pending", factory_unit=old, boxes=boxes)
    with pytest.raises(_Reached) as reached:
        run(_move_to(new)(conn))
    assert str(reached.value).startswith("UPDATE cfpl_rtv_header SET factory_unit = $1")


def test_every_header_field_can_be_corrected_while_undecided():
    fields = dict(customer="ACME FOODS", invoice_number="INV-9", challan_no="CH-1", dn_no="DN-1",
                  sales_poc="Shubham Seth", sales_poc_email="", business_head="Prashant Pal",
                  remark="fixed", vehicle_number="MH01", transporter_name="T", driver_name="D",
                  inward_manager="M")
    conn = Conn("On Hold")
    with pytest.raises(_Reached) as reached:
        run(create_service.update_cr(conn, COMPANY, CR, schemas.CRHeaderUpdate(**fields)))
    set_clause = str(reached.value).split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    assert [a.split(" = ")[0] for a in set_clause.split(", ")] == [*fields, "updated_at"]
