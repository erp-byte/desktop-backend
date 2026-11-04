"""The form saves a return in one step (C9), and an edit re-sends it (C3).

PUT /{company}/{cr_id}/save writes the header, the lines and the boxes in one
transaction: a refusal or failure part-way — a printed article taken off, a box
printed on another page, a figure the server refuses — saves none of it, where the
form's three separate calls used to leave the header saved and the rest not. A new
return's boxes go in with its create the same way.

When a save changes the figures of a return already sent for approval, it is sent
again on its own, saying why, and the earlier links no longer decide it; Save & send
sends it whatever changed.

ReturnDb extends test_cr_approval_decisions' FakeConn with the return's lines and
boxes, and a transaction that puts everything back when it is left by an exception,
as PostgreSQL's rollback does — so "nothing was saved" is checked on the data, not
taken from the code.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_one_step_save.py
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import cold_stock_service, create_service, query_service
from app.modules.customer_returns.services import wa_notify as wa
from tests.services.test_cr_approval_decisions import (  # noqa: F401 — fixtures
    BH, BH_EMAIL, COMPANY, CR, TOKEN_KEY, FakeConn, client, header, mail, no_guard_errors, tokens,
)

HEADER_ID = 501
ACTOR = "warehouse.w202@candorfoods.in"
SAVE = f"/api/v1/customer-returns/{COMPANY}/{CR}/save"
CASHEW, ALMOND = "CASHEW W320 10KG", "ALMOND 25KG"


def stored_line(article=CASHEW, qty=4, rate=950.0, **over):
    row = {"item_description": article, "material_type": "RM", "item_category": "Nuts", "sub_category": "Cashew",
           "sale_group": None, "uom": "10", "qty": qty, "rate": rate, "value": qty * rate, "conversion": "10",
           "net_weight": 40.0, "carton_weight": 0.0, "lot_number": None, "item_mark": None, "spl_remarks": None,
           "vakkal": None}
    row.update(over)
    return row


def stored_box(article=CASHEW, number=1, box_id=None, **over):
    row = {"article_description": article, "box_number": number, "box_id": box_id, "uom": "10",
           "conversion": "10", "net_weight": 10, "gross_weight": 11, "count": 1, "lot_number": None,
           "item_mark": None, "spl_remarks": None, "vakkal": None, "rtv_line_id": None}
    row.update(over)
    return row


def full_header(**over):
    return header(**{"rtv_date": None, "dn_no": None, "conversion": 0, "remark": None, "vehicle_number": None,
                     "transporter_name": None, "driver_name": None, "inward_manager": None,
                     "created_ts": None, "updated_at": None, **over})


class ReturnDb(FakeConn):
    """One return's header, lines and boxes, answered statement by statement; anything
    else goes to FakeConn (the routing, the users, the approval log, WhatsApp)."""

    def __init__(self, *, status="Pending", lines=None, boxes=None, **kw):
        super().__init__(full_header(status=status), lines=[], **kw)
        self.lines[CR] = [dict(x, id=i + 1, header_id=HEADER_ID)
                          for i, x in enumerate(lines if lines is not None else [stored_line()])]
        self.boxes = [dict(b, id=100 + i, header_id=HEADER_ID) for i, b in enumerate(boxes or [])]
        self._ids = 1000

    # ── a transaction that rolls back ──
    def transaction(self):
        db = self

        class _Tx:
            async def __aenter__(self):
                self.saved = copy.deepcopy((db.headers, db.lines, db.boxes, db.approval_log))
                return self

            async def __aexit__(self, exc_type, *exc):
                if exc_type is not None:
                    db.headers, db.lines, db.boxes, db.approval_log = self.saved
                return False

        return _Tx()

    def _next_id(self):
        self._ids += 1
        return self._ids

    def version(self):
        rows = sorted((b["article_description"], b["box_number"], b["box_id"] or "-", str(b["net_weight"]),
                       str(b["gross_weight"]), str(b["count"]), b["lot_number"] or "-") for b in self.boxes)
        return hashlib.md5(repr(rows).encode()).hexdigest()

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if s == "SELECT item_description FROM cfpl_rtv_lines WHERE header_id = $1":
            self._log("fetch", sql, args)
            return [{"item_description": x["item_description"]} for x in self._rows_of(args[0])[1]]
        if s == "SELECT id, item_description FROM cfpl_rtv_lines WHERE header_id = $1":
            self._log("fetch", sql, args)
            return [{"id": x["id"], "item_description": x["item_description"]} for x in self._rows_of(args[0])[1]]
        if s.startswith("SELECT article_description, COUNT(*) AS boxes, COUNT(NULLIF(btrim(box_id), '')) AS printed"):
            self._log("fetch", sql, args)
            out = {}
            for b in self.boxes:
                if b["header_id"] == args[0] and b["article_description"] in args[1]:
                    n = out.setdefault(b["article_description"], [0, 0])
                    n[0] += 1
                    n[1] += 1 if (b["box_id"] or "").strip() else 0
            return [{"article_description": a, "boxes": n, "printed": p} for a, (n, p) in out.items()]
        if s.startswith("SELECT id, article_description, box_number, box_id, ") and "FROM cfpl_rtv_boxes" in s:
            self._log("fetch", sql, args)
            return [dict(b) for b in self.boxes if b["header_id"] == args[0]]
        if s.startswith("SELECT h.rtv_id AS rtv_id, b.article_description"):
            self._log("fetch", sql, args)
            hid = self.headers[args[0]]["id"]
            return [{"rtv_id": args[0], "created_at": None, "updated_at": None, **b}
                    for b in sorted(self.boxes, key=lambda b: (b["article_description"], b["box_number"]))
                    if b["header_id"] == hid]
        if s.startswith("SELECT rtv_id, status, btrim(invoice_number)"):   # the duplicate-invoice check
            self._log("fetch", sql, args)
            return []
        return await super().fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO cfpl_rtv_header"):
            self._log("fetchrow", sql, args)
            (cand, unit, customer, invoice, challan, dn, conversion, poc, poc_email, bh, remark, vehicle,
             transporter, driver, inward, created_by) = args
            self.headers[cand] = full_header(
                id=HEADER_ID + 1, rtv_id=cand, factory_unit=unit, customer=customer, invoice_number=invoice,
                challan_no=challan, business_head=bh, created_by=created_by, sales_poc=poc, remark=remark)
            self.lines[cand] = []
            return {"id": HEADER_ID + 1}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT md5("):
            self._log("fetchval", sql, args)
            return self.version()
        if s == "SELECT to_regclass('public.box_edit_logs') IS NOT NULL":
            self._log("fetchval", sql, args)
            return False
        return await super().fetchval(sql, *args)

    def _rows_of(self, header_id):
        cr = next(k for k, h in self.headers.items() if h["id"] == header_id)
        return cr, self.lines[cr]

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        if s == "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)":
            self._log("execute", sql, args)
            return "SELECT 1"
        if s.startswith("INSERT INTO cfpl_rtv_lines"):
            self._log("execute", sql, args)
            cols = ("header_id", "item_description", "material_type", "item_category", "sub_category",
                    "sale_group", "uom", "qty", "rate", "value", "conversion", "net_weight", "carton_weight",
                    "lot_number", "item_mark", "spl_remarks", "vakkal")
            row = dict(zip(cols, args), id=self._next_id())
            self._rows_of(row["header_id"])[1].append(row)
            return "INSERT 0 1"
        if s == "DELETE FROM cfpl_rtv_lines WHERE header_id = $1":
            self._log("execute", sql, args)
            self._rows_of(args[0])[1].clear()
            return "DELETE"
        if s.startswith("UPDATE cfpl_rtv_boxes b SET rtv_line_id = l.id"):
            self._log("execute", sql, args)
            ids = {x["item_description"]: x["id"] for x in self._rows_of(args[0])[1]}
            for b in self.boxes:
                b["rtv_line_id"] = ids.get(b["article_description"])
            return "UPDATE"
        if s.startswith("DELETE FROM cfpl_rtv_boxes WHERE header_id = $1 AND article_description = ANY"):
            self._log("execute", sql, args)
            before = len(self.boxes)
            self.boxes = [b for b in self.boxes if b["article_description"] not in args[1]]
            return f"DELETE {before - len(self.boxes)}"
        if s.startswith("DELETE FROM cfpl_rtv_boxes WHERE header_id = $1 AND article_description = $2"):
            self._log("execute", sql, args)
            self.boxes = [b for b in self.boxes if (b["article_description"], b["box_number"]) != args[1:3]]
            return "DELETE 1"
        if s.startswith("UPDATE cfpl_rtv_boxes SET uom = COALESCE($4, uom)"):
            self._log("execute", sql, args)
            (_, art, num, uom, conv, net, gross, lot, mark, remarks, vakkal, count, line_id) = args
            b = next(b for b in self.boxes if (b["article_description"], b["box_number"]) == (art, num))
            for k, v in (("uom", uom), ("conversion", conv), ("net_weight", net), ("gross_weight", gross),
                         ("lot_number", lot), ("item_mark", mark), ("spl_remarks", remarks), ("vakkal", vakkal),
                         ("count", count), ("rtv_line_id", b["rtv_line_id"] or line_id)):
                if v is not None:
                    b[k] = v
            return "UPDATE 1"
        if s.startswith("INSERT INTO cfpl_rtv_boxes"):
            self._log("execute", sql, args)
            (header_id, line_id, art, num, uom, conv, net, gross, lot, mark, remarks, vakkal, count) = args
            self.boxes.append({"id": self._next_id(), "header_id": header_id, "rtv_line_id": line_id,
                               "article_description": art, "box_number": num, "box_id": None, "uom": uom,
                               "conversion": conv, "net_weight": net or 0, "gross_weight": gross or 0,
                               "lot_number": lot, "item_mark": mark, "spl_remarks": remarks, "vakkal": vakkal,
                               "count": count})
            return "INSERT 0 1"
        return await super().execute(sql, *args)


def run(coro):
    return asyncio.run(coro)


def form_line(article=CASHEW, qty="4", rate="950"):
    return {"material_type": "RM", "item_category": "Nuts", "sub_category": "Cashew", "item_description": article,
            "uom": "10", "qty": qty, "rate": rate, "net_weight": "40"}


def form_box(article=CASHEW, number=1, box_id=None, net="10", gross="11"):
    return {"article_description": article, "box_number": number, "box_id": box_id, "uom": "10",
            "net_weight": net, "gross_weight": gross, "count": 1}


def request(*, lines=None, boxes=None, base_version=None, send=False, **head):
    h = {"factory_unit": "W202", "customer": "Sample Foods Pvt Ltd", "invoice_number": "INV-2044",
         "business_head": "Prashant Pal", **head}
    return schemas.CRSaveRequest(header=h, lines=lines or [form_line()], boxes=boxes,
                                 base_version=base_version, send=send)


def save(db, req, **kw):
    return run(create_service.save_cr(db, COMPANY, CR, req, actor=ACTOR, **kw))


def snapshot(db):
    return copy.deepcopy((db.headers, db.lines, db.boxes, db.approval_log))


# ── one transaction ──────────────────────────────────────────────────────────
def test_a_save_writes_the_header_lines_and_boxes_together():
    db = ReturnDb(boxes=[stored_box(box_id="12345678-1")])
    res = save(db, request(remark="two lots", lines=[form_line(qty="5"), form_line(ALMOND, "2", "400")],
                           boxes=[form_box(box_id="12345678-1"), form_box(ALMOND, 1)],
                           base_version=db.version()))
    assert db.headers[CR]["remark"] == "two lots"
    assert [(x["item_description"], x["qty"], x["value"]) for x in db.lines[CR]] == [(CASHEW, 5, 4750.0),
                                                                                    (ALMOND, 2, 800.0)]
    assert sorted((b["article_description"], b["box_id"]) for b in db.boxes) == [(ALMOND, None),
                                                                                 (CASHEW, "12345678-1")]
    # Every box points at its re-created line.
    ids = {x["item_description"]: x["id"] for x in db.lines[CR]}
    assert all(b["rtv_line_id"] == ids[b["article_description"]] for b in db.boxes)
    assert (res["status"], res["lines_count"], res["inserted"], res["updated"], res["box_version"]) == (
        "saved", 2, 1, 1, db.version())
    assert res["figures_changed"] is True
    assert res["figures_version"] == query_service.figures_version(
        {**db.headers[CR], "lines": [{**x, "qty": str(x["qty"])} for x in db.lines[CR]]})


@pytest.mark.parametrize("why, req, code", [
    ("a printed article taken off", lambda db: request(lines=[form_line(ALMOND)], boxes=[]), "article_has_labels"),
    ("a label the page doesn't know", lambda db: request(lines=[form_line(qty="9")],
                                                         boxes=[form_box(box_id="99999999-1")]), "box_changed"),
    ("boxes changed on another page", lambda db: request(lines=[form_line(qty="9")], boxes=[],
                                                         base_version="stale"), "boxes_changed"),
])
def test_a_refusal_part_way_saves_nothing(why, req, code):
    db = ReturnDb(boxes=[stored_box(box_id="12345678-1")])
    before = snapshot(db)
    with pytest.raises(HTTPException) as exc:
        save(db, req(db))
    assert exc.value.status_code == 409 and exc.value.detail["error"] == code, why
    assert snapshot(db) == before, f"{why}: something was saved"


def test_a_failure_after_the_header_was_written_saves_nothing():
    db = ReturnDb()
    db.fail_on = "INSERT INTO cfpl_rtv_lines"
    before = snapshot(db)
    with pytest.raises(ConnectionError):
        save(db, request(remark="changed", lines=[form_line(qty="7")], boxes=[]))
    assert snapshot(db) == before


def test_a_bad_figure_is_refused_before_anything_is_read_or_written():
    db = ReturnDb()
    with pytest.raises(HTTPException) as exc:
        save(db, request(lines=[form_line(qty="2.5")], boxes=[form_box()]))
    assert exc.value.status_code == 422 and db.calls == []
    with pytest.raises(HTTPException):
        save(db, request(boxes=[form_box(net="12", gross="11")]))
    assert db.calls == []


def test_a_decided_return_is_not_saved():
    db = ReturnDb(status="Approved")
    before = snapshot(db)
    with pytest.raises(HTTPException) as exc:
        save(db, request(lines=[form_line(qty="9")], boxes=[]))
    assert exc.value.detail["error"] == "already_decided" and snapshot(db) == before


def test_without_boxes_the_boxes_are_left_as_they_are():
    db = ReturnDb(boxes=[stored_box(), stored_box(number=2)])
    save(db, request(lines=[form_line(qty="6")]))
    assert len(db.boxes) == 2


def test_a_printed_box_left_out_is_kept_and_an_unprinted_one_goes():
    db = ReturnDb(boxes=[stored_box(box_id="12345678-1"), stored_box(number=2)])
    res = save(db, request(boxes=[], base_version=db.version()))
    assert [b["box_number"] for b in db.boxes] == [1] and (res["kept_printed"], res["deleted"]) == (1, 1)


def test_a_cold_return_ims_put_in_cold_stock_keeps_its_boxes(monkeypatch):
    async def _ims_rows(conn, tables, cr_id):
        return 3

    monkeypatch.setattr(cold_stock_service, "ims_rows", _ims_rows)
    db = ReturnDb(boxes=[stored_box(box_id="12345678-1")])
    db.headers[CR]["factory_unit"] = "Savla D-39"
    # Its header and lines can still be corrected while the boxes stay as they are...
    save(db, request(factory_unit="Savla D-39", remark="ok", boxes=[form_box(box_id="12345678-1")]))
    assert db.headers[CR]["remark"] == "ok"
    # ...but a save that would change them is refused, and saves nothing.
    before = snapshot(db)
    with pytest.raises(HTTPException) as exc:
        save(db, request(factory_unit="Savla D-39", remark="changed again",
                         boxes=[form_box(box_id="12345678-1", net="9")]))
    assert exc.value.detail["error"] == "cold_stock_from_ims" and snapshot(db) == before


def test_a_figure_change_is_recorded_with_the_save_and_a_remark_is_not():
    db = ReturnDb()
    assert save(db, request(remark="call before delivery", boxes=[]))["figures_changed"] is False
    assert db.approval_log == []
    res = save(db, request(lines=[form_line(qty="5")], boxes=[]))
    (logged,) = db.approval_log
    assert (logged["event"], logged["figures"], logged["actor"]) == ("changed", res["figures_version"], ACTOR)


# ── a new return's boxes go in with it ───────────────────────────────────────
def test_a_new_return_is_created_with_its_boxes_in_one_transaction():
    db = ReturnDb()
    data = schemas.CRCreate(company=COMPANY, header={"factory_unit": "W202", "customer": "New Foods",
                                                     "business_head": "Prashant Pal"},
                            lines=[form_line()], boxes=[form_box(), form_box(number=2)])
    run(create_service.create_cr(db, COMPANY, data, "warehouse.w202@candorfoods.in"))
    new = next(k for k in db.headers if k != CR)
    assert [x["item_description"] for x in db.lines[new]] == [CASHEW]
    assert [(b["header_id"], b["box_number"], b["box_id"]) for b in db.boxes] == [(HEADER_ID + 1, 1, None),
                                                                                (HEADER_ID + 1, 2, None)]


def test_a_new_return_s_box_can_t_arrive_printed_and_then_nothing_is_created():
    db = ReturnDb()
    before = snapshot(db)
    data = schemas.CRCreate(company=COMPANY, header={"factory_unit": "W202", "customer": "New Foods",
                                                     "business_head": "Prashant Pal"},
                            lines=[form_line()], boxes=[form_box(box_id="12345678-1")])
    with pytest.raises(HTTPException) as exc:
        run(create_service.create_cr(db, COMPANY, data, "warehouse.w202@candorfoods.in"))
    assert exc.value.detail["error"] == "box_changed" and snapshot(db) == before


# ── through the route: the request after the save ────────────────────────────
@pytest.fixture
def no_whatsapp(monkeypatch):
    monkeypatch.setattr(wa, "_wa_enabled", lambda: False)


def _body(req):
    return req.model_dump(mode="json")


def test_save_and_send_sends_it_after_the_save(client, mail, no_whatsapp):
    db = ReturnDb()
    res = client(db, BH).put(SAVE, json=_body(request(boxes=[], send=True)))
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["sent_because"], body["approval"]["status"]) == ("requested", "sent")
    assert mail.sent and [e["event"] for e in db.approval_log] == ["sent"]
    # Save & send needs what the Send needs.
    assert ("customer_returns", "returns", "create") in {a[:3] for a in client.asked}


def test_a_figure_change_on_a_sent_return_re_sends_it_and_old_links_stop_working(client, tokens, mail, no_whatsapp):
    sent_at = datetime.now(timezone.utc) - timedelta(hours=1)
    old = query_service.figures_version({**full_header(), "lines": [{**stored_line(), "qty": "4"}]})
    db = ReturnDb(approval_log=[{"company": COMPANY, "rtv_id": CR, "event": "sent", "figures": old,
                                 "actor": ACTOR, "at": sent_at, "outcome": None}])
    old_link = tokens(COMPANY, CR, "approve", BH_EMAIL, old)
    res = client(db, BH).put(SAVE, json=_body(request(lines=[form_line(qty="5")], boxes=[])))
    body = res.json()
    assert (body["figures_changed"], body["sent_because"], body["approval"]["status"]) == (True, "figures_changed",
                                                                                         "sent")
    assert "changed this customer return, so it is sent again with its new figures" in mail.sent[0]["text"]
    new_links = {jwt.decode(t.split("token=")[1], TOKEN_KEY, algorithms=["HS256"])["figures"]
                 for m in mail.sent for t in m["text"].split() if "email-action?token=" in t}
    assert new_links == {body["figures_version"]} != {old}
    assert [e["event"] for e in db.approval_log] == ["sent", "changed", "sent"]
    # The link from before the edit no longer decides it.
    refused = client(db, auth=False).post("/api/v1/customer-returns/email-action", data={"token": old_link})
    assert refused.status_code == 409 and db.headers[CR]["status"] == "Pending"


def test_a_save_that_changes_no_figure_sends_nothing(client, mail, no_whatsapp):
    db = ReturnDb(approval_log=[{"company": COMPANY, "rtv_id": CR, "event": "sent", "figures": "x",
                                 "actor": ACTOR, "at": datetime(2026, 10, 25, tzinfo=timezone.utc), "outcome": None}])
    body = client(db, BH).put(SAVE, json=_body(request(remark="note only", boxes=[]))).json()
    assert (body["figures_changed"], body["approval"], body["sent_because"]) == (False, None, None)
    assert mail.sent == []


def test_a_return_never_sent_is_not_sent_by_an_edit(client, mail, no_whatsapp):
    db = ReturnDb()
    body = client(db, BH).put(SAVE, json=_body(request(lines=[form_line(qty="5")], boxes=[]))).json()
    assert (body["figures_changed"], body["approval"]) == (True, None) and mail.sent == []


def test_a_return_on_hold_counts_as_sent(client, mail, no_whatsapp):
    db = ReturnDb(status="On Hold")
    body = client(db, BH).put(SAVE, json=_body(request(lines=[form_line(qty="5")], boxes=[]))).json()
    assert body["sent_because"] == "figures_changed" and mail.sent


def test_the_save_route_needs_edit_and_boxes_permissions(client):
    db = ReturnDb()
    res = client(db, BH, allowed=False).put(SAVE, json=_body(request(boxes=[])))
    assert res.status_code == 403
    assert len(db.lines[CR]) == 1 and db.headers[CR]["remark"] is None
