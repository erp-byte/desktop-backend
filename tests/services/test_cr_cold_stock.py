"""A cold return's cartons in cold stock (customer_returns/services/cold_stock_service.py).

Once a return to a cold store is approved, each printed box the ERP wrote becomes one
row of the return company's cold stock, made once and recorded in cr_cold_stock_rows;
later box edits are carried to it, and nothing else about the row is touched. Tested
against an in-memory database (ColdDb) that answers the service's statements.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cr_cold_stock.py
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.modules.customer_returns.services import approval_service, cold_stock_service

COMPANY = "CFPL"
CR = "CR-20261028153000"
ART = "ALMOND W-320"
COLD = "cfpl_cold_stocks"
LINKS = "cr_cold_stock_rows"
ERP_DAY = datetime(2026, 10, 28, 10, 0)       # naive UTC, as NOW() writes it: an ERP box
IMS_DAY = datetime(2026, 10, 17, 10, 0)       # before IMS closed: an IMS box
NEEDED = sorted(cold_stock_service._NEEDED)
OPTIONAL = ["cold_item_mark", "spl_remarks", "inward_transaction_no", "auto_created_from_inward"]
_PH = re.compile(r"\$(\d+)(?:::\w+)?")


def _arg(token: str, args: tuple):
    m = _PH.fullmatch(token.strip())
    if m:
        return args[int(m[1]) - 1]
    assert token.strip() == "NOW()", token
    return "NOW()"


class ColdDb:
    """One cfpl_rtv_header row with its lines and boxes, cfpl_cold_stocks and
    cr_cold_stock_rows; answers exactly the statements the service sends."""

    def __init__(self, unit="Savla D-39", status="Approved", *, cold_cols=None, links_table=True,
                 cold_table=True):
        self.header = {"id": 501, "rtv_id": CR, "rtv_date": datetime(2026, 10, 28, 20, 0),
                       "factory_unit": unit, "customer": "ACME FOODS", "status": status}
        self.lines = [{"item_description": ART, "item_category": "Nuts", "sub_category": "Almond",
                       "rate": Decimal("500.00"), "lot_number": "LINE-LOT", "item_mark": "LINE-MARK",
                       "vakkal": "LINE-VAKKAL", "spl_remarks": None}]
        self.boxes: list[dict] = []
        self.cold: dict[int, dict] = {}
        self.links: dict[int, dict] = {}
        self.cold_cols = set(cold_cols if cold_cols is not None else NEEDED + OPTIONAL)
        self.tables = {t for t, there in ((COLD, cold_table), (LINKS, links_table)) if there}
        self.calls: list[str] = []
        self.locked_at: int | None = None

    # ── fixtures ──
    def box(self, number, box_id, net="25.000", *, created=ERP_DAY, article=ART, **values):
        b = {"article_description": article, "box_number": number, "box_id": box_id,
             "net_weight": Decimal(net), "created_at": created,
             **{k: values.get(k) for k in ("lot_number", "item_mark", "vakkal", "spl_remarks")}}
        self.boxes.append(b)
        return b

    def ims_row(self, box_id):
        rid = max(self.cold, default=0) + 1
        self.cold[rid] = {"id": rid, "box_id": box_id, "transaction_no": CR, "inward_transaction_no": CR,
                          "auto_created_from_inward": True}
        return rid

    def writes(self):
        return [s for s in self.calls if re.match(r"(INSERT|UPDATE|DELETE)\b", s)]

    def row_of(self, box_id) -> dict:
        (row,) = [r for r in self.cold.values() if r.get("box_id") == box_id]
        return row

    # ── the connection ──
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
        return s

    async def fetchrow(self, sql, *args):
        s = self._log(sql)
        if s == "SELECT id, rtv_date, factory_unit, customer FROM cfpl_rtv_header WHERE rtv_id = $1":
            h = self.header
            return {k: h[k] for k in ("id", "rtv_date", "factory_unit", "customer")} if args[0] == CR else None
        if s.startswith("UPDATE cfpl_rtv_header SET status = $2, approved_by = $3"):
            if approval_service._status(self.header["status"]) not in args[4]:
                return None
            self.header["status"] = args[1]
            return {"rtv_id": CR, "status": args[1]}
        if s == "SELECT status FROM cfpl_rtv_header WHERE rtv_id = $1":
            return {"status": self.header["status"]}
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def fetchval(self, sql, *args):
        s = self._log(sql)
        if s == "SELECT to_regclass($1::text) IS NOT NULL":
            return args[0] in self.tables
        m = re.fullmatch(r"SELECT COUNT\(\*\) FROM (\w+) WHERE inward_transaction_no = \$1 "
                         r"AND auto_created_from_inward IS TRUE( AND NOT EXISTS .+)?", s)
        if m:
            assert m[1] == COLD
            linked = {k["cold_stock_id"] for k in self.links.values() if k["cold_table"] == args[1]} if m[2] else set()
            return sum(1 for r in self.cold.values() if r.get("inward_transaction_no") == args[0]
                       and r.get("auto_created_from_inward") is True and r["id"] not in linked)
        m = re.fullmatch(r"INSERT INTO (\w+) \((.+?)\) VALUES \((.+?)\) RETURNING id", s)
        if m:
            assert m[1] == COLD and COLD in self.tables
            cols, vals = m[2].split(", "), m[3].split(", ")
            assert len(cols) == len(vals) and set(cols) <= self.cold_cols, cols
            rid = max(self.cold, default=0) + 1
            self.cold[rid] = {"id": rid, **{c: _arg(v, args) for c, v in zip(cols, vals)}}
            return rid
        raise AssertionError(f"unexpected fetchval: {s}")

    async def fetch(self, sql, *args):
        s = self._log(sql)
        if s.startswith("SELECT column_name FROM information_schema.columns"):
            return [{"column_name": c} for c in sorted(self.cold_cols)] if args[0] == COLD else []
        if s.startswith(f"SELECT id, box_id, cold_stock_id, net_weight, lot_no, item_mark, vakkal, "
                        f"spl_remarks FROM {LINKS} WHERE company = $1 AND rtv_id = $2"):
            return [dict(k) for k in self.links.values() if (k["company"], k["rtv_id"]) == args]
        if s.startswith("SELECT b.box_id, b.created_at, b.net_weight,"):
            assert args == (self.header["id"],)
            out = []
            for b in sorted(self.boxes, key=lambda b: (b["article_description"], b["box_number"])):
                line = next((l for l in self.lines if l["item_description"] == b["article_description"]), None)
                if line is None or not (b["box_id"] or "").strip():
                    continue
                pick = lambda k, lk=None: b[k] if b[k] is not None else line[lk or k]  # noqa: E731
                out.append({"box_id": b["box_id"], "created_at": b["created_at"], "net_weight": b["net_weight"],
                            "lot_no": pick("lot_number"), "item_mark": pick("item_mark"),
                            "vakkal": pick("vakkal"), "spl_remarks": pick("spl_remarks"),
                            **{k: line[k] for k in ("item_description", "item_category", "sub_category", "rate")}})
            return out
        raise AssertionError(f"unexpected fetch: {s}")

    async def execute(self, sql, *args):
        s = self._log(sql)
        if s == "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)":
            assert args == (f"crbox:{COMPANY}:{CR}",)
            if self.locked_at is None:
                self.locked_at = len(self.calls) - 1
            return "SELECT 1"
        m = re.fullmatch(rf"INSERT INTO {LINKS} \((.+?)\) VALUES \((.+?)\)", s)
        if m:
            assert LINKS in self.tables
            cols, vals = m[1].split(", "), m[2].split(", ")
            row = {c: _arg(v, args) for c, v in zip(cols, vals)}
            assert not any((k["company"], k["rtv_id"], k["box_id"]) == (row["company"], row["rtv_id"], row["box_id"])
                           for k in self.links.values()), "UNIQUE (company, rtv_id, box_id)"
            lid = max(self.links, default=0) + 1
            self.links[lid] = {"id": lid, **row}
            return "INSERT 0 1"
        m = re.fullmatch(r"UPDATE (\w+) SET (.+) WHERE id = \$1", s)
        if m:
            store = {COLD: self.cold, LINKS: self.links}[m[1]]
            row = store.get(args[0])
            if row is None:
                return "UPDATE 0"
            for assignment in m[2].split(", "):
                col, value = assignment.split(" = ")
                assert m[1] != COLD or col in self.cold_cols, col
                row[col] = _arg(value, args)
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {s}")


def run(coro):
    return asyncio.run(coro)


def sync(db):
    return run(cold_stock_service.sync(db, COMPANY, CR))


# ── what a row is ────────────────────────────────────────────────────────────
def test_each_printed_box_becomes_one_cold_stock_row_as_ims_wrote_it():
    db = ColdDb()
    db.box(1, "11111111-1", "25.000", lot_number="BOX-LOT")
    db.box(2, "11111111-2", "24.500", item_mark="BOX-MARK", spl_remarks="torn")
    db.box(3, None)                                     # not printed: no label, no row
    db.box(4, "   ")                                    # blank is no label
    assert sync(db) == {"added": 2, "corrected": 0}
    one, two = db.row_of("11111111-1"), db.row_of("11111111-2")
    assert {k: v for k, v in one.items() if k != "id"} == {
        "inward_dt": date(2026, 10, 29),                # 28 Oct 20:00 UTC is 29 Oct in IST
        "unit": "D-39", "storage_location": "Savla D-39",
        "inward_no": CR, "transaction_no": CR, "box_id": "11111111-1",
        "inward_transaction_no": CR, "auto_created_from_inward": True,
        "item_description": ART, "group_name": "Nuts", "item_subgroup": "Almond",
        "exporter": "ACME FOODS", "no_of_cartons": Decimal(1),
        "weight_kg": Decimal("25.000"), "total_inventory_kgs": Decimal("25.000"),
        "last_purchase_rate": Decimal("500.00"), "value": Decimal("12500.00"),
        "lot_no": "BOX-LOT", "item_mark": "LINE-MARK", "cold_item_mark": "LINE-MARK",
        "vakkal": "LINE-VAKKAL", "spl_remarks": None,
    }
    assert (two["lot_no"], two["item_mark"], two["spl_remarks"], two["value"]) == (
        "LINE-LOT", "BOX-MARK", "torn", Decimal("12250.00"))
    assert sorted((k["box_id"], k["cold_table"], k["cold_stock_id"], k["net_weight"]) for k in db.links.values()) == [
        ("11111111-1", COLD, one["id"], Decimal("25.000")), ("11111111-2", COLD, two["id"], Decimal("24.500"))]
    assert all((k["company"], k["rtv_id"]) == (COMPANY, CR) for k in db.links.values())


def test_rows_are_made_under_the_return_s_box_lock():
    db = ColdDb()
    db.box(1, "11111111-1")
    sync(db)
    first_write = next(i for i, s in enumerate(db.calls) if re.match(r"(INSERT|UPDATE)\b", s))
    assert db.locked_at is not None and db.locked_at < first_write


@pytest.mark.parametrize("unit, code, store", [
    ("Savla D-39", "D-39", "Savla D-39"), ("Old Savla", "D-39", "Savla D-39"), ("D-514", "D-514", "Savla D-514"),
    ("Savla Bond", "Savla Bond", "Savla Bond"), ("Rishi Cold", "Rishi", "Rishi"),
    ("Supreme Cold", "Supreme", "Supreme"), ("Eskimo", "Eskimo", "Eskimo")])
def test_the_row_names_the_store_as_ims_did_in_any_spelling(unit, code, store):
    db = ColdDb(unit)
    db.box(1, "11111111-1")
    sync(db)
    assert (db.row_of("11111111-1")["unit"], db.row_of("11111111-1")["storage_location"]) == (code, store)


@pytest.mark.parametrize("unit", ["W202", "A185 Cold", "Cold Storage", "Savla", None, ""])
def test_a_return_to_no_cold_store_puts_nothing_in_cold_stock(unit):
    db = ColdDb(unit)
    db.box(1, "11111111-1")
    assert sync(db) == {"added": 0, "corrected": 0}
    assert db.calls == ["SELECT id, rtv_date, factory_unit, customer FROM cfpl_rtv_header WHERE rtv_id = $1"]


def test_a_missing_return_puts_nothing_in_cold_stock():
    db = ColdDb()
    assert run(cold_stock_service.sync(db, COMPANY, "CR-NOPE")) == {"added": 0, "corrected": 0}
    assert db.writes() == []


def test_a_box_whose_article_has_no_line_is_not_copied():
    db = ColdDb()
    db.box(1, "11111111-1", article="NOT ON THE RETURN")
    assert sync(db)["added"] == 0 and db.cold == {}


# ── made once, then only corrected ───────────────────────────────────────────
def test_a_second_sync_changes_nothing():
    db = ColdDb()
    db.box(1, "11111111-1")
    sync(db)
    before = len(db.writes())
    assert sync(db) == {"added": 0, "corrected": 0}
    assert len(db.writes()) == before and len(db.cold) == 1


def test_a_box_printed_after_approval_is_added():
    db = ColdDb()
    db.box(1, "11111111-1")
    sync(db)
    db.box(2, "22222222-2")
    assert sync(db) == {"added": 1, "corrected": 0}
    assert len(db.cold) == 2


def test_a_box_edit_is_carried_to_its_row_and_nothing_else_is_touched():
    db = ColdDb()
    box = db.box(1, "11111111-1", "25.000", lot_number="BOX-LOT")
    sync(db)
    row = db.row_of("11111111-1")
    # an inner cold transfer relabels and moves the row inside the store
    row.update(lot_no="RELABELLED", storage_location="Savla D-39 Chamber 2")
    box["net_weight"] = Decimal("23.750")
    assert sync(db) == {"added": 0, "corrected": 1}
    assert (row["weight_kg"], row["total_inventory_kgs"], row["value"]) == (
        Decimal("23.750"), Decimal("23.750"), Decimal("11875.00"))
    assert (row["lot_no"], row["storage_location"]) == ("RELABELLED", "Savla D-39 Chamber 2")
    (link,) = db.links.values()
    assert link["net_weight"] == Decimal("23.750") and link["updated_at"] == "NOW()"
    # now the box's own lot and mark are changed: those are carried too
    box.update(lot_number="NEW-LOT", item_mark="NEW-MARK")
    assert sync(db) == {"added": 0, "corrected": 1}
    assert (row["lot_no"], row["item_mark"], row["cold_item_mark"]) == ("NEW-LOT", "NEW-MARK", "NEW-MARK")
    assert row["weight_kg"] == Decimal("23.750")


def test_a_weight_written_differently_is_not_an_edit():
    db = ColdDb()
    box = db.box(1, "11111111-1", "25.000")
    sync(db)
    box["net_weight"] = Decimal("25")
    assert sync(db) == {"added": 0, "corrected": 0}


def test_a_row_that_has_gone_is_not_made_again(caplog):
    db = ColdDb()
    box = db.box(1, "11111111-1")
    sync(db)
    db.cold.clear()
    box["net_weight"] = Decimal("20.000")
    with caplog.at_level(logging.WARNING):
        assert sync(db) == {"added": 0, "corrected": 0}
    assert db.cold == {} and "is gone" in caplog.text
    (link,) = db.links.values()
    assert link["net_weight"] == Decimal("25.000")


# ── IMS's boxes and rows are left alone ──────────────────────────────────────
def test_a_box_ims_wrote_is_never_copied():
    db = ColdDb()
    db.box(1, "11111111-1", created=IMS_DAY)
    db.box(2, "11111111-2", created=None)
    db.box(3, "11111111-3", created=ERP_DAY)
    assert sync(db)["added"] == 1
    assert [r["box_id"] for r in db.cold.values()] == ["11111111-3"]


def test_a_return_with_ims_s_rows_is_left_as_it_is():
    db = ColdDb()
    db.ims_row("11111111-1")
    db.box(1, "11111111-1")
    db.box(2, "11111111-2")
    assert sync(db) == {"added": 0, "corrected": 0}
    assert len(db.cold) == 1 and db.links == {}


def test_the_erp_s_own_rows_are_not_taken_for_ims_s():
    db = ColdDb()
    db.box(1, "11111111-1")
    sync(db)
    tables = {"header": "cfpl_rtv_header"}
    assert run(cold_stock_service.ims_rows(db, tables, CR)) == 0
    db.ims_row("11111111-9")
    assert run(cold_stock_service.ims_rows(db, tables, CR)) == 1


def test_without_the_record_table_every_return_row_counts_as_ims_s():
    db = ColdDb(links_table=False)
    db.ims_row("11111111-1")
    assert run(cold_stock_service.ims_rows(db, {"header": "cfpl_rtv_header"}, CR)) == 1
    assert run(cold_stock_service.ims_rows(ColdDb(cold_table=False), {"header": "cfpl_rtv_header"}, CR)) == 0


def test_one_box_id_on_two_boxes_is_copied_once(caplog):
    db = ColdDb()
    db.box(1, "11111111-1")
    db.box(2, "11111111-1")
    with caplog.at_level(logging.WARNING):
        assert sync(db)["added"] == 1
    assert len(db.cold) == 1 and "on two boxes" in caplog.text


def test_the_box_lock_is_one_lock_whatever_the_company_s_case():
    from app.modules.customer_returns.services import query_service
    assert query_service.box_lock_key("cfpl", CR) == query_service.box_lock_key(" CFPL ", CR) == f"crbox:CFPL:{CR}"


# ── a database without what the row needs ───────────────────────────────────
@pytest.mark.parametrize("kw, missing", [
    ({"links_table": False}, LINKS), ({"cold_table": False}, COLD),
    ({"cold_cols": [c for c in NEEDED if c != "weight_kg"]}, "weight_kg")])
def test_nothing_is_copied_without_the_tables_and_columns_a_row_needs(kw, missing, caplog):
    db = ColdDb(**kw)
    db.box(1, "11111111-1")
    with caplog.at_level(logging.WARNING):
        assert sync(db) == {"added": 0, "corrected": 0}
    assert db.writes() == [] and missing in caplog.text


def test_columns_a_cold_table_lacks_are_left_out_of_the_row():
    db = ColdDb(cold_cols=NEEDED)
    box = db.box(1, "11111111-1", spl_remarks="torn", item_mark="M")
    sync(db)
    row = db.row_of("11111111-1")
    assert not set(OPTIONAL) & set(row)
    # without IMS's marks no row can be IMS's: never asked (the COUNT would name them)
    assert not any("auto_created_from_inward" in s for s in db.calls)
    box.update(spl_remarks="re-taped", item_mark="M2")
    assert sync(db)["corrected"] == 1
    assert row["item_mark"] == "M2" and "spl_remarks" not in row and "cold_item_mark" not in row


# ── approval puts an approved cold return in cold stock ──────────────────────
@pytest.fixture
def authorised(monkeypatch):
    async def _authorise(conn, company, cr_id, actor_email):
        h = conn.header       # seen_status: what the check read, when it differs from now
        return {"status": getattr(conn, "seen_status", h["status"]), "factory_unit": h["factory_unit"],
                "created_by": "w@x.in"}, [{"name": "BH", "email": actor_email}]

    monkeypatch.setattr(approval_service, "_authorise", _authorise)


def _decide(db, action):
    return run(approval_service.apply_cr_action(db, COMPANY, CR, action, "bh@x.in"))


def test_approving_a_cold_return_puts_its_cartons_in_cold_stock(authorised):
    db = ColdDb(status="Pending")
    db.box(1, "11111111-1")
    db.box(2, None)
    assert _decide(db, "approve")["status"] == "Approved"
    assert [r["box_id"] for r in db.cold.values()] == ["11111111-1"]
    # the box lock first, then the decision, then the copy: a box write takes the lock
    # and then the header row, so the two can't deadlock
    update = next(i for i, s in enumerate(db.calls) if s.startswith("UPDATE cfpl_rtv_header"))
    first_copy = next(i for i, s in enumerate(db.calls) if s.startswith("INSERT INTO cfpl_cold_stocks"))
    assert db.locked_at < update < first_copy


@pytest.mark.parametrize("action", ["reject", "hold"])
def test_rejecting_or_holding_a_cold_return_puts_nothing_in_cold_stock(authorised, action):
    db = ColdDb(status="Pending")
    db.box(1, "11111111-1")
    _decide(db, action)
    assert db.cold == {} and db.locked_at is None


def test_approving_a_warehouse_return_never_touches_cold_stock(authorised):
    db = ColdDb("W202", status="Pending")
    db.box(1, "11111111-1")
    assert _decide(db, "approve")["status"] == "Approved"
    assert [s for s in db.calls if not s.startswith("UPDATE cfpl_rtv_header")] == []


def test_an_approval_that_lost_the_race_copies_nothing(authorised):
    db = ColdDb(status="Pending")
    db.box(1, "11111111-1")
    db.seen_status = "Pending"
    db.header["status"] = "Rejected"            # decided between the check and the UPDATE
    assert _decide(db, "approve")["already_actioned"] is True
    assert db.cold == {}
