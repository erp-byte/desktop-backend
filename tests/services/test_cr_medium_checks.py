"""Customer returns: the medium loopholes closed on 31 Oct (wave 1).

  C4  the business head must be someone who can approve the return
  C6  a customer's invoice already on another return is warned about, saved on confirm
  C7  quantity is a whole number: a decimal is refused, never cut
  C8  numbers are numbers, never negative; value is quantity x rate, worked out here
  C10 an article taken off a return takes its unprinted boxes; printed ones refuse it
  C11 customer, factory unit and article can't be blank; the unit must be a known site
  B8  a new label id is one no box table holds
  B9  a save lists the printed labels it made out of date (reprint)
  B11 Approved · Boxed exactly while every article has a box and every box a label
  B12 box weights and count never negative; net no more than gross
  B13 the server records what changed on a printed box, and every first print
  B14 the edit-log endpoint takes entries only for the return's own boxes and fields

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_medium_checks.py
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.modules.customer_returns import schemas
from app.modules.customer_returns.services import box_service, create_service, validate
from tests.services.test_cr_write_access import BOX, COMPANY, CR, Conn, _Reached  # noqa: F401
from tests.services.test_cr_write_access import _label_ids_are_free  # noqa: F401  (autouse fixture)

ART = BOX["article_description"]


def run(coro):
    return asyncio.run(coro)


def refused(coro_or_fn, *args) -> HTTPException:
    with pytest.raises(HTTPException) as e:
        out = coro_or_fn(*args) if args else coro_or_fn
        if asyncio.iscoroutine(out):
            run(out)
    return e.value


def line(**over):
    base = dict(material_type="RM", item_category="Nuts", sub_category="Almond", item_description=ART,
                uom="KG", qty="4", rate="10")
    base.update(over)
    return schemas.CRLineCreate(**base)


# ── C7 · C8: numbers ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, want", [("4", 4), (" 1,200 ", 1200), ("1,20,000", 120000), ("", 0), (None, 0),
                                       ("12.5", Decimal("12.5"))])
def test_a_number_is_read_as_typed(raw, want):
    assert validate.number(raw, "Rate") == want


@pytest.mark.parametrize("raw, code", [("abc", "invalid_number"), ("1.2.3", "invalid_number"), ("NaN", "invalid_number"),
                                       ("-1", "negative_number"), ("12,5", "invalid_number")])
def test_a_bad_number_is_refused_not_read_as_zero(raw, code):
    e = refused(validate.number, raw, "Rate of X")
    assert e.status_code == 422 and e.detail["error"] == code and "Rate of X" in e.detail["message"]


@pytest.mark.parametrize("qty", ["2.5", "0.75"])
def test_a_decimal_quantity_is_refused_not_cut(qty):
    e = refused(validate.lines, [line(qty=qty)])
    assert e.detail["error"] == "whole_number_only" and ART in e.detail["message"]


def test_value_is_quantity_times_rate_whatever_the_browser_sent():
    (nums,) = validate.lines([line(qty="1,200", rate="2.5", value="999999")])
    assert nums == {"qty": 1200, "rate": 2.5, "value": 3000.0, "net_weight": 0.0, "carton_weight": 0.0}


# ── C10 · C11: lines and header ──────────────────────────────────────────────
@pytest.mark.parametrize("over, code", [({"item_description": "  "}, "required"), ({"uom": ""}, "required"),
                                        ({"rate": "-5"}, "negative_number"), ({"net_weight": "x"}, "invalid_number")])
def test_a_line_missing_what_it_needs_is_refused(over, code):
    assert refused(validate.lines, [line(**over)]).detail["error"] == code


def test_the_same_article_twice_is_refused_in_any_case_or_spacing():
    e = refused(validate.lines, [line(), line(item_description="almond   w-320")])
    assert e.detail["error"] == "duplicate_article"


def head(**over):
    base = dict(factory_unit="D-39", customer="ACME FOODS", business_head="Prashant Pal")
    base.update(over)
    return schemas.CRHeaderCreate(**base)


@pytest.mark.parametrize("over, code", [({"customer": " "}, "required"), ({"factory_unit": ""}, "required"),
                                        ({"factory_unit": "Savla"}, "unknown_factory_unit"),
                                        ({"conversion": "two"}, "invalid_number")])
def test_a_header_missing_what_it_needs_is_refused(over, code):
    assert refused(validate.header, head(**over)).detail["error"] == code


@pytest.mark.parametrize("unit", ["D-39", "Savla D-39", "Old Savla", "A-185", "Warehouse A185", "Supreme Cold"])
def test_every_spelling_of_a_known_unit_is_accepted(unit):
    validate.header(head(factory_unit=unit))


def test_an_edit_may_leave_an_old_return_s_unknown_unit_as_it_was():
    validate.header(schemas.CRHeaderUpdate(factory_unit="Savla"), stored_unit="Savla", partial=True)
    with pytest.raises(HTTPException) as e:
        validate.header(schemas.CRHeaderUpdate(factory_unit="Nowhere"), stored_unit="Savla", partial=True)
    assert e.value.detail["error"] == "unknown_factory_unit"


# ── B12: box values ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("over, code", [({"net_weight": "-1"}, "negative_number"), ({"gross_weight": "-0.5"}, "negative_number"),
                                        ({"count": -2}, "negative_number"),
                                        ({"net_weight": "26", "gross_weight": "25.5"}, "net_above_gross")])
def test_a_box_with_impossible_values_is_refused_before_anything_is_written(over, code):
    conn = Conn("Pending")
    e = refused(box_service.upsert_box(conn, COMPANY, CR, schemas.CRBoxUpsertRequest(**{**BOX, **over})))
    assert e.status_code == 422 and e.detail["error"] == code
    assert conn.calls == []
    e = refused(box_service.bulk_save_boxes(conn, COMPANY, CR, schemas.CRBulkBoxUpdateRequest(
        boxes=[schemas.CRBulkBoxItem(**{**BOX, **over})])))
    assert e.detail["error"] == code and conn.calls == []


def test_a_box_not_yet_weighed_gross_may_carry_a_net_weight():
    validate.box(schemas.CRBoxUpsertRequest(**{**BOX, "net_weight": "10", "gross_weight": "0"}))


# ── C4 · C6: business head and duplicate invoice (create and edit) ────────────
ROUTING_PEOPLE = [{"name": "Prashant Pal", "email": "prashant.pal@candorfoods.in", "role_name": "business_head"},
                  {"name": "No Mail", "email": "", "role_name": "business_head"}]


class HeadDb:
    """Answers the business-head lookup (mail_service.fetch_routing), the duplicate-invoice
    read and the update's reads; stops at the first write."""

    def __init__(self, *, deputies=(), others=(), stored=None):
        self.deputies, self.others = list(deputies), list(others)
        self.stored = stored
        self.calls: list[str] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return _Tx()

    def _log(self, sql):
        s = " ".join(sql.split())
        self.calls.append(s)
        if re.match(r"(INSERT|UPDATE|DELETE)\b", s):
            raise _Reached(s)
        return s

    async def fetch(self, sql, *args):
        s = self._log(sql)
        if s.startswith("SELECT DISTINCT u.full_name AS name"):
            return ROUTING_PEOPLE
        if s.startswith("SELECT kind, match_key, match_type, email FROM cr_email_routing"):
            return [{"kind": "deputy_approver", "match_key": n, "match_type": "name", "email": e}
                    for n, e in self.deputies]
        if s.startswith("SELECT rtv_id, status, btrim(invoice_number)"):
            self.dup_args = args
            return self.others
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchrow(self, sql, *args):
        s = self._log(sql)
        if s.startswith("SELECT id, customer, invoice_number, challan_no, business_head, created_by FROM cfpl_rtv_header"):
            return self.stored
        if s.startswith("SELECT id, status, factory_unit FROM cfpl_rtv_header WHERE rtv_id = $1 FOR UPDATE"):
            return {"id": 501, "status": "Pending", "factory_unit": "D-39"}
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")


def create(db, h, **kw):
    return create_service.create_cr(db, COMPANY, schemas.CRCreate(company=COMPANY, header=h, lines=[line()]),
                                    "w@x.in", **kw)


@pytest.mark.parametrize("bh, code", [(None, "required"), ("  ", "required"), ("No Mail", "business_head_has_no_email"),
                                      ("Somebody Else", "business_head_has_no_email")])
def test_a_business_head_who_can_t_approve_is_refused(bh, code):
    e = refused(create(HeadDb(), head(business_head=bh)))
    assert e.status_code == 422 and e.detail["error"] == code


def test_raising_a_return_in_your_own_name_needs_a_deputy_to_approve_it():
    me = "prashant.pal@candorfoods.in"
    req = schemas.CRCreate(company=COMPANY, header=head(), lines=[line()])
    e = refused(create_service.create_cr(HeadDb(), COMPANY, req, me))
    assert e.detail["error"] == "no_other_approver"
    with pytest.raises(_Reached):          # with a deputy it is created
        run(create_service.create_cr(HeadDb(deputies=[("Deputy D", "d@x.in")]), COMPANY, req, me))


def test_an_invoice_already_on_another_return_is_warned_about_naming_it():
    db = HeadDb(others=[{"rtv_id": "CR-20261020100000", "status": "Approved", "invoice_number": "INV-9",
                         "challan_no": None}])
    e = refused(create(db, head(invoice_number="INV-9")))
    assert e.status_code == 409 and e.detail["error"] == "duplicate_invoice"
    assert e.detail["message"] == "Invoice INV-9 of ACME FOODS is already on CR-20261020100000 (Approved)."
    assert db.dup_args == (None, "ACME FOODS", "INV-9", "")
    sql = next(c for c in db.calls if c.startswith("SELECT rtv_id, status, btrim(invoice_number)"))
    assert "<> 'Rejected'" in sql, "a rejected return's invoice may be returned again"
    with pytest.raises(_Reached) as reached:     # confirmed: saved, and not even asked again
        run(create(HeadDb(others=db.others), head(invoice_number="INV-9"), allow_duplicate_invoice=True))
    assert str(reached.value).startswith("INSERT INTO cfpl_rtv_header")


def test_a_return_with_no_invoice_or_challan_is_never_a_duplicate():
    db = HeadDb(others=[{"rtv_id": "CR-1", "status": "Pending", "invoice_number": None, "challan_no": None}])
    with pytest.raises(_Reached):
        run(create(db, head()))
    assert not any(c.startswith("SELECT rtv_id, status, btrim(invoice_number)") for c in db.calls)


STORED = {"id": 501, "customer": "ACME FOODS", "invoice_number": "INV-9", "challan_no": None,
          "business_head": "No Mail", "created_by": "w@x.in"}


def test_an_edit_that_keeps_an_old_business_head_and_invoice_checks_neither():
    db = HeadDb(stored=STORED, others=[{"rtv_id": "CR-2", "status": "Pending", "invoice_number": "INV-9",
                                        "challan_no": None}])
    with pytest.raises(_Reached) as reached:
        run(create_service.update_cr(db, COMPANY, CR, schemas.CRHeaderUpdate(
            business_head="No Mail", customer="ACME FOODS", invoice_number="INV-9", remark="x")))
    assert str(reached.value).startswith("UPDATE cfpl_rtv_header SET")
    assert not any("auth_user" in c or "btrim(invoice_number)" in c for c in db.calls)


def test_an_edit_that_changes_them_checks_both():
    e = refused(create_service.update_cr(HeadDb(stored=STORED), COMPANY, CR,
                                         schemas.CRHeaderUpdate(business_head="Somebody Else")))
    assert e.detail["error"] == "business_head_has_no_email"
    db = HeadDb(stored=STORED, others=[{"rtv_id": "CR-2", "status": "On Hold", "invoice_number": "INV-10",
                                        "challan_no": None}])
    e = refused(create_service.update_cr(db, COMPANY, CR, schemas.CRHeaderUpdate(invoice_number="INV-10")))
    assert e.detail["error"] == "duplicate_invoice" and db.dup_args == (501, "ACME FOODS", "INV-10", "")


# ── C10: taking an article off a return ──────────────────────────────────────
class LinesDb(Conn):
    """Conn plus the lines edit's reads: the stored lines and their boxes per article."""

    def __init__(self, *, old_lines, boxes):
        super().__init__("Pending", stop=False, box_rows=[(a, n, bid) for a, n, bid in boxes])
        self.old_lines = old_lines

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT item_description FROM cfpl_rtv_lines WHERE header_id = $1"):
            self.calls.append(s)
            return [{"item_description": a} for a in self.old_lines]
        if s.startswith("SELECT article_description, COUNT(*) AS boxes"):
            self.calls.append(s)
            out = []
            for art in args[1]:
                mine = [b for b in self.box_rows if b["article_description"] == art]
                if mine:
                    out.append({"article_description": art, "boxes": len(mine),
                                "printed": sum(1 for b in mine if (b["box_id"] or "").strip())})
            return out
        return await super().fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT id, status, factory_unit FROM cfpl_rtv_header WHERE rtv_id = $1 FOR UPDATE"):
            self.calls.append(s)
            return {"id": 501, "status": "Pending", "factory_unit": "W202"}
        return await super().fetchrow(sql, *args)


def edit_lines(db, *articles):
    return create_service.update_cr_lines(db, COMPANY, CR, schemas.CRLinesUpdateRequest(
        lines=[line(item_description=a) for a in articles]))


def test_an_article_with_printed_labels_can_t_be_removed_or_renamed():
    db = LinesDb(old_lines=[ART, "CASHEW W-240"], boxes=[(ART, 1, "12345678-1"), (ART, 2, None)])
    e = refused(edit_lines(db, "ALMOND W-320 (renamed)", "CASHEW W-240"))
    assert e.status_code == 409 and e.detail["error"] == "article_has_labels"
    assert ART in e.detail["message"] and "1 printed" in e.detail["message"]
    assert not any(re.match(r"(INSERT|UPDATE|DELETE)\b", c) for c in db.calls)


def test_an_article_taken_off_takes_its_unprinted_boxes_and_says_so():
    db = LinesDb(old_lines=[ART, "CASHEW W-240"], boxes=[("CASHEW W-240", 1, None), ("CASHEW W-240", 2, " ")])
    out = run(edit_lines(db, ART))
    (delete,) = [c for c in db.calls if c.startswith("DELETE FROM cfpl_rtv_boxes")]
    assert "article_description = ANY($2::text[])" in delete
    assert out["status"] == "updated" and out["lines_count"] == 1 and out["box_version"] == "v1"


def test_an_edit_that_keeps_every_article_touches_no_box():
    db = LinesDb(old_lines=[ART], boxes=[(ART, 1, "12345678-1"), ("ORPHAN", 1, "87654321-1")])
    out = run(edit_lines(db, ART))
    assert out["boxes_removed"] == 0
    assert not any(c.startswith("DELETE FROM cfpl_rtv_boxes") for c in db.calls)


# ── B11: Approved · Boxed ────────────────────────────────────────────────────
@pytest.mark.parametrize("boxed, status", [(True, "Submitted"), (False, "Approved")])
def test_an_approved_return_is_boxed_exactly_while_every_article_has_a_labelled_box(boxed, status):
    class Db(Conn):
        async def fetchval(self, sql, *args):
            if sql.split()[:2] == ["SELECT", "EXISTS"]:
                self.calls.append(" ".join(sql.split()))
                return boxed
            return await super().fetchval(sql, *args)

    db = Db("Approved", stop=False)
    run(box_service.bulk_save_boxes(db, COMPANY, CR, schemas.CRBulkBoxUpdateRequest(boxes=[BOX])))
    (update,) = [c for c in db.calls if c.startswith("UPDATE cfpl_rtv_header")]
    assert "btrim(status) IN ('Approved', 'Submitted')" in update      # never an undecided or rejected one
    assert db.last_args(update) == (CR, status)


def test_printing_one_by_one_reaches_boxed_too():
    db = Conn("Approved", stop=False)
    run(box_service.upsert_box(db, COMPANY, CR, schemas.CRBoxUpsertRequest(**BOX)))
    assert any(c.startswith("UPDATE cfpl_rtv_header SET status = $2") for c in db.calls)


# ── B13 · B9: what the server records, and the labels to reprint ──────────────
def _printed(**values):
    db = Conn("Approved", stop=False, box_rows=[(ART, 1, "12345678-1")])
    db.box_rows[0].update({"net_weight": Decimal("10.000"), "gross_weight": Decimal("10.400"), "lot_number": "L1",
                           "count": 4, "item_mark": "M", "vakkal": "V", "uom": "KG", **values})
    return db


def logs(db):
    return [a for s, a in db.recorded if s.startswith("INSERT INTO box_edit_logs")]


def test_a_save_records_what_it_changed_on_a_printed_box_and_lists_it_for_reprint():
    db = _printed()
    out = run(box_service.bulk_save_boxes(db, COMPANY, CR, schemas.CRBulkBoxUpdateRequest(boxes=[
        schemas.CRBulkBoxItem(**{**BOX, "box_id": "12345678-1", "net_weight": "9.750", "lot_number": "L1",
                                 "vakkal": "V2"})]), actor="w@x.in"))
    entries = {(a[4], a[5], a[6]) for a in logs(db)}
    assert entries == {("net_weight", "10.000", "9.750"), ("vakkal", "V", "V2")}
    assert all(a[0] == "w@x.in" and a[2] == CR and a[3] == "12345678-1" for a in logs(db))
    assert out["reprint"] == [{"article_description": ART, "box_number": 1, "box_id": "12345678-1"}]


def test_a_change_the_label_doesn_t_show_is_recorded_but_needs_no_reprint():
    db = _printed()
    out = run(box_service.bulk_save_boxes(db, COMPANY, CR, schemas.CRBulkBoxUpdateRequest(boxes=[
        schemas.CRBulkBoxItem(**{**BOX, "box_id": "12345678-1", "net_weight": "10", "vakkal": "V2"})])))
    assert [a[4] for a in logs(db)] == ["vakkal"] and out["reprint"] == []


def test_an_unchanged_save_records_nothing():
    db = _printed()
    out = run(box_service.bulk_save_boxes(db, COMPANY, CR, schemas.CRBulkBoxUpdateRequest(boxes=[
        schemas.CRBulkBoxItem(**{**BOX, "box_id": "12345678-1", "net_weight": "10.000"})])))
    assert logs(db) == [] and out["reprint"] == []


def test_a_first_print_and_a_reprint_are_recorded():
    db = Conn("Pending", stop=False)
    out = run(box_service.upsert_box(db, COMPANY, CR, schemas.CRBoxUpsertRequest(**BOX), actor="w@x.in"))
    (entry,) = logs(db)
    assert entry[1] == f"Printed label {out['box_id']}" and entry[4] == "printed" and entry[6] == out["box_id"]
    db = _printed()
    run(box_service.upsert_box(db, COMPANY, CR, schemas.CRBoxUpsertRequest(
        **{**BOX, "box_id": "12345678-1", "net_weight": "11"})))
    assert [(a[4], a[5], a[6]) for a in logs(db)] == [("net_weight", "10.000", "11")]


# ── B8: a new label id ───────────────────────────────────────────────────────
def test_a_label_id_another_box_holds_is_never_issued(monkeypatch):
    taken = iter([True, True, False])
    seen = []

    async def box_id_taken(conn, box_id):
        seen.append(box_id)
        return next(taken)

    monkeypatch.setattr(box_service.box_identify_service, "box_id_taken", box_id_taken)
    out = run(box_service.upsert_box(Conn("Pending", stop=False), COMPANY, CR, schemas.CRBoxUpsertRequest(**BOX)))
    assert len(seen) == 3 and out["box_id"] == seen[-1] and out["box_id"].endswith("-1")


def test_a_reprint_asks_for_no_new_id(monkeypatch):
    async def never(conn, box_id):
        raise AssertionError("a reprint keeps its label")

    monkeypatch.setattr(box_service.box_identify_service, "box_id_taken", never)
    out = run(box_service.upsert_box(_printed(), COMPANY, CR,
                                     schemas.CRBoxUpsertRequest(**{**BOX, "box_id": "12345678-1"})))
    assert out["box_id"] == "12345678-1"


def test_no_free_id_is_a_retry_message_not_a_shared_label(monkeypatch):
    async def always(conn, box_id):
        return True

    monkeypatch.setattr(box_service.box_identify_service, "box_id_taken", always)
    e = refused(box_service.upsert_box(Conn("Pending", stop=False), COMPANY, CR, schemas.CRBoxUpsertRequest(**BOX)))
    assert e.status_code == 503 and e.detail["error"] == "box_id_unavailable"


# ── B14: the edit-log endpoint ───────────────────────────────────────────────
class LogDb:
    def __init__(self, owned=True):
        self.owned, self.inserts = owned, []

    def transaction(self):
        return Conn("Pending").transaction()

    async def fetchval(self, sql, *args):
        assert "cfpl_rtv_boxes" in sql and "cdpl_rtv_boxes" in sql      # either company's return
        return self.owned

    async def execute(self, sql, *args):
        self.inserts.append(args)


def entry(field="lot_number", box_id="12345678-1"):
    return schemas.CRBoxEditLogRequest(email_id="x", box_id=box_id, rtv_id=CR,
                                       changes=[schemas.CRBoxEditLogEntry(field_name=field, old_value="a", new_value="b")])


@pytest.mark.parametrize("field", ["deleted", "box_id", "transaction_no"])
def test_the_log_takes_no_entry_that_isn_t_a_box_field(field):
    db = LogDb()
    e = refused(box_service.log_box_edits(db, entry(field), email_id="w@x.in"))
    assert e.status_code == 422 and e.detail["error"] == "unknown_box_field" and db.inserts == []


def test_the_log_takes_no_entry_for_a_box_not_on_the_return():
    db = LogDb(owned=False)
    e = refused(box_service.log_box_edits(db, entry(), email_id="w@x.in"))
    assert e.status_code == 404 and e.detail["error"] == "box_not_on_return" and db.inserts == []


def test_the_log_takes_an_entry_for_the_return_s_own_box():
    db = LogDb()
    assert run(box_service.log_box_edits(db, entry(), email_id="w@x.in")) == {"status": "logged", "entries": 1}
    assert db.inserts[0][0] == "w@x.in" and db.inserts[0][2:5] == (CR, "12345678-1", "lot_number")
