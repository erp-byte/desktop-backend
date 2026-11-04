"""The Inner Cold transfer (D4) and its undo (X-02), against the SQL-evaluating fake
database of test_transfer_box_lookup_cold (with the FOR UPDATE / ORDER BY support of
test_transfer_dispatch_lock), which here also rolls a failed transaction back, as
Postgres does. The box rule R3 (lookup_service.unavailable_boxes) is stubbed: its own
cases are test_transfer_box_rules'; here, which boxes it is asked about and what is done
with its answer.

What is pinned:
  - one cold store: the From must be a cold store (400) the user may work in (403; admins
    and users with no lock excepted); the picked row must be at it, by its unit, else its
    location (409); a new location naming another site, 'Cold Storage' too, is 409
    'Moving boxes to another cold store has to be an inter-unit transfer.', a name that is
    no site 409; lines added to a saved challan must be for its store (409);
  - the boxes the user picked: the picked row first, then the rest of its cold-search
    group (item, lot, inward, item_mark, location, unit) in id order, never rows of
    another store or group; the line's company names the table, else the store does;
  - R3: a row whose box R3 does not place at the store is left alone and not counted;
    R3 is asked about the group's real boxes (trimmed keys) from the store;
  - a row with a box id moves whole (a quantity that would split one is a line error); a
    row without one splits, its new row taking its own fields, weight_kg per carton
    (total and value shared by cartons, so the two rows add up to the old one);
  - canonical names: the From and a new location are stored by their canonical names;
  - atomic: a line that cannot be done writes nothing, whichever line of the batch it is;
  - every changed or inserted row is recorded; deleting the challan restores each
    changed row, deletes each inserted one, and its lines and records, in one
    transaction; it is refused (409, nothing changed) when a row has changed since or is
    gone (naming a later inner cold transfer that changed it), when a box of it has gone
    on an inter-unit transfer since (R3), for a challan saved before the records existed,
    and for a user locked to another store (403);
  - the list and the read are scoped to the user's sites (any spelling);
  - migration 124 (static): the change table the service writes, after 123.

Run:  PYTHONPATH=. python -m pytest tests/services/test_inner_cold_transfer.py
"""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.middleware.request_context import AuthError
from app.core.sites import scope_keys, site_key
from app.core.warehouse_scope import WAREHOUSE_NORM_ANY_SQL
from app.modules.transfer import schemas
from app.modules.transfer.services import inner_cold_service as svc
from app.modules.transfer.services import lookup_service
from tests.services import test_transfer_dispatch_lock as dl
from tests.services import test_transfer_request_decisions as trd

CHALLAN = "ICT202610161400"
COLD_TABLES = ("cfpl_cold_stocks", "cdpl_cold_stocks")
ADMIN = trd._user("admin@candorfoods.in", is_admin=True, warehouses=["W202"])
SAVLA = trd._user("store.d39@candorfoods.in", warehouses=["D-39"])        # the grant code of Savla D-39
RISHI = trd._user("store.rishi@candorfoods.in", warehouses=["Rishi"])
ANYONE = trd._user("anyone@candorfoods.in")                               # no warehouse lock


# ── The fake database ────────────────────────────────────────────────────────
class _Tx:
    """A transaction: the tables as they were are put back when it fails."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        if not self.db.tx_depth:
            self.db.saved = copy.deepcopy(self.db.tables)
        self.db.tx_depth += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.db.tx_depth -= 1
        if not self.db.tx_depth and exc_type is not None:
            self.db.tables.clear()
            self.db.tables.update(self.db.saved)
        return False


class ColdDB(dl.DispatchDB):
    """dl's fake plus a rollback on failure, the group read's ORDER BY id FOR UPDATE, and
    the literal to_regclass of inner_cold_service._table_exists."""

    def transaction(self):
        return _Tx(self)

    def _run(self, kind, sql, args):
        s = " ".join(sql.split())
        if s == "SELECT to_regclass('public.inner_cold_transfer')":
            self.calls.append((kind, s, args))
            return [{"to_regclass": "inner_cold_transfer" if "inner_cold_transfer" in self.tables else None}], None
        if s.endswith(" ORDER BY id FOR UPDATE"):
            assert self.tx_depth, "row locked outside a transaction"
            self.locked.append((s, args))
            return super()._run(kind, s.removesuffix(" FOR UPDATE"), args)
        return super()._run(kind, sql, args)


def _row(id, *, unit="D-39", location="Savla", lot="L-1", box=None, cartons=1, kg="25.000", **over) -> dict:
    """A cold-stock row: one box (box None: its own id; '' for none) of ALMOND 25KG, lot
    `lot`, inward INW-1, mark RK, at `unit` / `location` (Savla D-39 by default)."""
    box_id = f"3473{id:04d}-1" if box is None else box
    row = {
        "id": id, "inward_dt": date(2026, 9, 1), "unit": unit, "inward_no": "INW-1",
        "item_description": "ALMOND 25KG", "item_mark": "RK", "vakkal": "V-1", "lot_no": lot,
        "no_of_cartons": Decimal(cartons), "weight_kg": Decimal(kg),
        "total_inventory_kgs": Decimal(kg) * cartons, "group_name": "NUTS", "item_subgroup": "ALMOND",
        "storage_location": location, "exporter": "ACME AGRO", "last_purchase_rate": Decimal("800.00"),
        "value": Decimal("20000.00") * cartons, "box_id": box_id,
        "transaction_no": "TR-2026-0001" if box_id else None,
    }
    row.update(over)
    return row


def _db(cfpl=(), cdpl=(), *, lines=(), changes=True) -> ColdDB:
    tables = {"cfpl_cold_stocks": [dict(r) for r in cfpl], "cdpl_cold_stocks": [dict(r) for r in cdpl],
              "inner_cold_transfer": [dict(r) for r in lines]}
    if changes:
        tables["inner_cold_transfer_changes"] = []
    return ColdDB(**tables)


def _line(record, qty, *, old="L-1", new="L-2", location=None, company=None) -> schemas.InnerTransferLine:
    return schemas.InnerTransferLine(
        stock_record_id=record, company=company, item_category="NUTS", item_description="ALMOND 25KG",
        net_weight=25, quantity=qty, old_lot_number=old, new_lot_number=new, new_storage_location=location)


def _payload(*lines, frm="Savla D-39", challan=CHALLAN) -> schemas.InnerTransferPayload:
    return schemas.InnerTransferPayload(
        company="cfpl",          # what the old page sent, whatever the stock: only a preference, the store decides
        header=schemas.InnerTransferHeader(challan_no=challan, transfer_name="16-10-2026", from_warehouse=frm,
                                           remark="RELABEL", reason_code="Inventory Balancing"),
        lines=list(lines))


def _create(db, payload, user=SAVLA) -> dict:
    return asyncio.run(svc.create_inner_transfer(db, payload, user))


def _delete(db, challan=CHALLAN, user=ADMIN) -> dict:
    return asyncio.run(svc.delete_inner_cold(db, challan, user))


def _cold(db) -> dict:
    return copy.deepcopy({t: db.tables[t] for t in COLD_TABLES})


def _lots(db, table="cfpl_cold_stocks") -> dict:
    return {r["id"]: r["lot_no"] for r in db.tables[table]}


def _written(db) -> list:
    """The audit and change rows the create wrote."""
    return db.tables["inner_cold_transfer"] + db.tables.get("inner_cold_transfer_changes", [])


@pytest.fixture(autouse=True)
def r3(monkeypatch):
    """lookup_service.unavailable_boxes, stubbed: the (box_id, transaction_no) keys put in
    `r3.held` are refused, as R3 refuses a box in transit or received elsewhere; each call
    is recorded as (boxes, from_warehouse, transfer_id)."""
    held: set = set()
    calls: list = []

    async def unavailable_boxes(conn, boxes, *, from_warehouse=None, transfer_id=None):
        boxes = list(boxes)
        calls.append((boxes, from_warehouse, transfer_id))
        return [{"box_id": b, "transaction_no": t, "reason": "in_transit", "transfer_id": 1,
                 "challan_no": "TRANS-1", "site": "Rishi"} for b, t in boxes if (b, t) in held]

    monkeypatch.setattr(lookup_service, "unavailable_boxes", unavailable_boxes)
    return SimpleNamespace(held=held, calls=calls)


# ── One cold store ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("location", ["Rishi", "Savla D-514", "Savla Bond", "Supreme Cold", "Eskimo",
                                      "Cold Storage", "W202", "Warehouse A185"])
def test_a_move_to_another_store_is_refused(location):
    db = _db([_row(10)])
    before = _cold(db)
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload(_line(10, 1, location=location)))
    assert (exc.value.status_code, exc.value.detail) == (
        409, "Moving boxes to another cold store has to be an inter-unit transfer.")
    assert _cold(db) == before and _written(db) == []


def test_a_new_location_that_is_no_site_is_refused():
    db = _db([_row(10)])
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload(_line(10, 1, location="Chamber 9")))
    assert exc.value.status_code == 409
    assert exc.value.detail == ("'Chamber 9' is not a location of Savla D-39; an inner cold transfer keeps its "
                                "boxes in Savla D-39, so pick Savla D-39 or no new location.")
    assert _written(db) == []


@pytest.mark.parametrize("frm", ["W202", "Cold Storage", "Savla", "Dev Int", ""])
def test_the_from_has_to_be_one_cold_store(frm):
    db = _db([_row(10)])
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload(_line(10, 1), frm=frm))
    assert exc.value.status_code == 400
    assert exc.value.detail.startswith("An inner cold transfer is made inside one cold store, picked as its From "
                                       "(Rishi, Savla D-39, Savla D-514, Savla Bond, Supreme, Eskimo); ")
    assert _written(db) == []


@pytest.mark.parametrize("user, allowed", [(SAVLA, True), (ADMIN, True), (ANYONE, True), (RISHI, False),
                                           (trd._user("w202@candorfoods.in", warehouses=["W202"]), False)],
                         ids=["granted-D-39", "admin", "no-lock", "rishi", "w202"])
def test_the_store_has_to_be_the_users(user, allowed):
    db = _db([_row(10)])
    if allowed:
        assert _create(db, _payload(_line(10, 1)), user)["status"] == "success"
        return
    with pytest.raises(AuthError) as exc:
        _create(db, _payload(_line(10, 1)), user)
    assert exc.value.status_code == 403
    assert exc.value.message.startswith("An inner cold transfer can only be made in your assigned warehouse (")
    assert exc.value.message.endswith("); this stock is at Savla D-39.")
    assert _written(db) == []


@pytest.mark.parametrize("row, at", [
    (_row(10, unit="Rishi", location="Rishi"), "Rishi"),
    (_row(10, unit="", location="Savla D-514"), "Savla D-514"),
    (_row(10, unit="Rishi", location="Savla D-39"), "Rishi"),        # the unit is read first
    (_row(10, unit="", location="Savla"), "no cold store the ERP knows (unit '', location 'Savla')"),
], ids=["another-store", "by-location", "unit-first", "no-store"])
def test_the_picked_row_has_to_be_at_the_from_store(row, at):
    # P1-21: a user of Savla D-39 cannot relabel another store's lot under a Savla challan.
    db = _db([row])
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload(_line(10, 1)))
    assert (exc.value.status_code, exc.value.detail) == (
        409, f"Line 1: the stock picked is at {at}, not Savla D-39; an inner cold transfer stays inside "
             "one cold store.")
    assert _written(db) == []


def test_lines_added_to_a_saved_challan_have_to_be_for_its_store():
    db = _db([_row(10)], [_row(20, unit="Rishi", location="Rishi"), _row(21, unit="Rishi", location="Rishi")])
    _create(db, _payload(_line(20, 1, company="cdpl"), frm="Rishi"), ANYONE)
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload(_line(10, 1)), ANYONE)
    assert (exc.value.status_code, exc.value.detail) == (
        409, f"Inner cold transfer {CHALLAN} is for Rishi; lines for Savla D-39 need an inner cold transfer "
             "of their own.")
    # The edit page adds lines under the challan's From, in any spelling.
    out = _create(db, _payload(_line(21, 1, old="L-1", new="L-3", company="cdpl"), frm="Rishi Cold"), ANYONE)
    assert out["status"] == "success"
    assert {r["from_warehouse"] for r in db.tables["inner_cold_transfer"]} == {"Rishi"}


def test_the_from_and_a_location_in_the_store_are_stored_by_their_canonical_names():
    db = _db([_row(10), _row(11)])
    out = _create(db, _payload(_line(10, 2, location="savla d39 cold"), frm="D-39"))
    assert out == {"status": "success", "updated_records": 1, "errors": [], "challan_no": CHALLAN}
    assert {(r["lot_no"], r["storage_location"], r["unit"]) for r in db.tables["cfpl_cold_stocks"]} == {
        ("L-2", "Savla D-39", "D-39")}
    (line,) = db.tables["inner_cold_transfer"]
    assert (line["from_warehouse"], line["new_storage_location"], line["old_storage_location"]) == (
        "Savla D-39", "Savla D-39", "Savla")
    assert (line["quantity"], line["net_weight_kg"], line["old_lot_number"], line["new_lot_number"]) == (
        2, Decimal("50.000"), "L-1", "L-2")


def test_without_a_new_location_only_the_lot_changes():
    db = _db([_row(10, location="Savla D-39 Chamber 3", unit="Savla D-39")])
    _create(db, _payload(_line(10, 1, new=" L-2 ")))
    (row,) = db.tables["cfpl_cold_stocks"]
    assert (row["lot_no"], row["storage_location"], row["unit"]) == ("L-2", "Savla D-39 Chamber 3", "Savla D-39")
    assert db.tables["inner_cold_transfer"][0]["new_storage_location"] is None


# ── The boxes the user picked ────────────────────────────────────────────────
def test_the_picked_row_moves_first_then_its_search_group_in_id_order():
    rows = [_row(10), _row(11), _row(12),
            _row(13, item_mark="XX"),                 # another mark: another search row
            _row(14, location="Savla D-39"),          # the same store spelled otherwise: another search row
            _row(15, unit="D-514"),                   # another store
            _row(16, inward_no="INW-2")]              # another inward
    db = _db(rows, [_row(17, unit="Rishi", location="Rishi")])
    assert _create(db, _payload(_line(11, 2)))["status"] == "success"
    assert _lots(db) == {10: "L-2", 11: "L-2", 12: "L-1", 13: "L-1", 14: "L-1", 15: "L-1", 16: "L-1"}
    assert _lots(db, "cdpl_cold_stocks") == {17: "L-1"}


def test_a_pick_at_one_store_never_moves_another_stores_rows():
    # The copy-or-move probe S3: a Rishi box picked, a Savla box of the same item / lot /
    # inward moved instead. Now only the picked store's rows move.
    db = _db([_row(301, lot="L-9")], [_row(302, unit="Rishi", location="Rishi", lot="L-9")])
    out = _create(db, _payload(_line(302, 1, old="L-9", company="cdpl"), frm="Rishi"), RISHI)
    assert out["status"] == "success"
    assert _lots(db) == {301: "L-9"} and _lots(db, "cdpl_cold_stocks") == {302: "L-2"}


def test_the_lines_company_names_the_table_and_without_it_the_store_does():
    # Both tables have a row 10: Savla's in cfpl, Rishi's in cdpl.
    rows = ([_row(10)], [_row(10, unit="Rishi", location="Rishi")])
    db = _db(*rows)
    _create(db, _payload(_line(10, 1), frm="Rishi"), ANYONE)
    assert (_lots(db), _lots(db, "cdpl_cold_stocks")) == ({10: "L-1"}, {10: "L-2"})

    db = _db(*rows)
    _create(db, _payload(_line(10, 1, company="cfpl")), ANYONE)
    assert (_lots(db), _lots(db, "cdpl_cold_stocks")) == ({10: "L-2"}, {10: "L-1"})

    db = _db(*rows)
    with pytest.raises(HTTPException) as exc:     # cfpl's row 10 is not at Rishi
        _create(db, _payload(_line(10, 1, company="cfpl"), frm="Rishi"), ANYONE)
    assert exc.value.status_code == 409 and "the stock picked is at Savla D-39, not Rishi" in exc.value.detail


@pytest.mark.parametrize("line, error", [
    (_line(None, 1), "Line 1: stock_record_id is required"),
    (_line(10, 0), "Line 1: quantity must be greater than 0"),
    (_line(10, 1, new="  "), "Line 1: the new lot number is required"),
    (_line(99, 1), "Line 1: record 99 not found in any cold table"),
    (_line(10, 1, old="L-0"), "Line 1: the stock picked is now lot L-1, not L-0; search for it again"),
])
def test_a_line_that_cannot_be_done_is_an_error_and_nothing_is_written(line, error):
    db = _db([_row(10)])
    before = _cold(db)
    assert _create(db, _payload(line)) == {"status": "error", "updated_records": 0, "errors": [error],
                                           "challan_no": CHALLAN}
    assert _cold(db) == before and _written(db) == []


def test_two_lines_can_take_from_one_lot():
    db = _db([_row(10), _row(11), _row(12)])
    out = _create(db, _payload(_line(10, 2, new="L-2"), _line(10, 1, new="L-3")))
    assert out["status"] == "success" and out["updated_records"] == 2
    assert _lots(db) == {10: "L-2", 11: "L-2", 12: "L-3"}


# ── R3 ───────────────────────────────────────────────────────────────────────
def test_a_box_r3_does_not_place_at_the_store_is_left_alone(r3):
    rows = [_row(10), _row(11, box=" 34730011-1 "), _row(12), _row(13, box="")]
    db = _db(rows)
    r3.held.add(("34730011-1", "TR-2026-0001"))
    assert _create(db, _payload(_line(10, 3)))["status"] == "success"
    assert _lots(db) == {10: "L-2", 11: "L-1", 12: "L-2", 13: "L-2"}
    # Asked once, from the store, about the group's real boxes (trimmed; a row without a
    # box id is never on a transfer).
    (call,) = r3.calls
    assert call[1:] == ("Savla D-39", None)
    assert sorted(call[0]) == [("34730010-1", "TR-2026-0001"), ("34730011-1", "TR-2026-0001"),
                               ("34730012-1", "TR-2026-0001")]


def test_a_box_r3_does_not_place_at_the_store_is_not_counted(r3):
    db = _db([_row(10), _row(11), _row(12)])
    r3.held.add(("34730011-1", "TR-2026-0001"))
    before = _cold(db)
    assert _create(db, _payload(_line(10, 3)))["errors"] == [
        "Line 1: transfer qty (3) exceeds available (2) at Savla D-39; 1 of its boxes are not counted, as "
        "their inter-unit transfers do not place them at Savla D-39"]
    assert _cold(db) == before and _written(db) == []


# ── A box moves whole; a row without one may split ──────────────────────────
@pytest.mark.parametrize("qty, asks", [(2, "3"), (4, "3 or 5")])
def test_a_box_is_moved_whole_never_split(qty, asks):
    db = _db([_row(10, cartons=3), _row(11, cartons=2)])
    before = _cold(db)
    out = _create(db, _payload(_line(10, qty)))
    box = "34730010-1" if qty == 2 else "34730011-1"
    held = "3" if qty == 2 else "2"
    assert out["errors"] == [f"Line 1: box {box} holds {held} cartons and a box moves whole, never split; "
                             f"ask for {asks} instead of {qty}."]
    assert _cold(db) == before and _written(db) == []


def test_a_row_without_a_box_id_splits_and_its_new_row_keeps_its_own_fields():
    picked = _row(10)                                    # a box of one carton
    loose = _row(11, box="", cartons=10, vakkal="V-9", exporter="OTHER EXPORTS", inward_dt=date(2026, 8, 1),
                 last_purchase_rate=Decimal("790.00"), value=Decimal("7900.00"), group_name="DRY FRUIT",
                 item_subgroup="ALMOND KERNEL")
    db = _db([picked, loose])
    assert _create(db, _payload(_line(10, 5)))["status"] == "success"
    rows = {r["id"]: r for r in db.tables["cfpl_cold_stocks"]}
    (new_id,) = set(rows) - {10, 11}
    new, kept = rows[new_id], rows[11]
    assert rows[10]["lot_no"] == "L-2"
    # weight_kg is per carton: both rows keep it; total and value are shared by cartons.
    assert (kept["lot_no"], kept["no_of_cartons"], kept["weight_kg"], kept["total_inventory_kgs"], kept["value"]) == (
        "L-1", 6, Decimal("25.000"), Decimal("150.000"), Decimal("4740.00"))
    assert (new["lot_no"], new["no_of_cartons"], new["weight_kg"], new["total_inventory_kgs"], new["value"]) == (
        "L-2", 4, Decimal("25.000"), Decimal("100.000"), Decimal("3160.00"))
    # Its own fields, not the picked row's (defect c), and no box identity.
    for col in ("inward_dt", "unit", "inward_no", "item_description", "item_mark", "vakkal", "group_name",
                "item_subgroup", "storage_location", "exporter", "last_purchase_rate"):
        assert new[col] == loose[col], col
    assert new["box_id"] is None and new["transaction_no"] is None
    (line,) = db.tables["inner_cold_transfer"]
    assert line["net_weight_kg"] == Decimal("125.000")     # 5 cartons of 25 kg


def test_a_line_that_cannot_be_done_rolls_back_the_lines_before_it():
    db = _db([_row(10), _row(11), _row(20, lot="L-5")])
    before = _cold(db)
    out = _create(db, _payload(_line(10, 2), _line(20, 2, old="L-5", new="L-6")))
    assert out == {"status": "error", "updated_records": 0, "challan_no": CHALLAN,
                   "errors": ["Line 2: transfer qty (2) exceeds available (1) at Savla D-39"]}
    assert _cold(db) == before and _written(db) == []


def test_without_the_change_table_nothing_is_saved():
    db = _db([_row(10)], changes=False)
    with pytest.raises(HTTPException) as exc:
        _create(db, _payload(_line(10, 1)))
    assert exc.value.status_code == 503 and "migration 124_inner_cold_transfer_changes.sql" in exc.value.detail
    assert db.tables["cfpl_cold_stocks"][0]["lot_no"] == "L-1"


def test_every_row_changed_or_inserted_is_recorded_with_its_values_before_and_after():
    db = _db([_row(10), _row(11, box="", cartons=4)])
    _create(db, _payload(_line(10, 3, location="Savla D-39")))
    (line,) = db.tables["inner_cold_transfer"]
    changes = db.tables["inner_cold_transfer_changes"]
    assert [(c["challan_no"], c["line_id"], c["cold_table"], c["kind"]) for c in changes] == [
        (CHALLAN, line["id"], "cfpl_cold_stocks", k) for k in ("update", "update", "insert")]
    whole, part, new = changes
    assert (whole["row_id"], whole["old_lot_no"], whole["old_storage_location"], whole["new_lot_no"],
            whole["new_storage_location"]) == (10, "L-1", "Savla", "L-2", "Savla D-39")
    assert (part["row_id"], part["old_no_of_cartons"], part["new_no_of_cartons"], part["new_lot_no"]) == (
        11, 4, 2, "L-1")
    assert new["old_lot_no"] is None and new["old_no_of_cartons"] is None
    assert (new["new_lot_no"], new["new_no_of_cartons"], new["new_storage_location"]) == ("L-2", 2, "Savla D-39")


# ── Deleting the challan undoes it ──────────────────────────────────────────
def test_deleting_the_challan_undoes_it():
    db = _db([_row(10), _row(11, box="", cartons=10), _row(12)])
    before = _cold(db)
    _create(db, _payload(_line(10, 5, location="Savla D-39")))
    assert _cold(db) != before
    out = _delete(db)
    assert out == {"success": True, "message": f"Inner cold transfer {CHALLAN} deleted and undone: 2 stock "
                                               "row(s) restored, 1 split row(s) removed."}
    assert _cold(db) == before
    assert db.tables["inner_cold_transfer"] == [] and db.tables["inner_cold_transfer_changes"] == []


def test_deleting_undoes_two_lines_that_took_from_one_lot():
    db = _db([_row(10), _row(11), _row(12)])
    before = _cold(db)
    _create(db, _payload(_line(10, 2, new="L-2"), _line(10, 1, new="L-3")))
    _delete(db)
    assert _cold(db) == before


def test_deleting_undoes_a_line_that_moved_rows_an_earlier_line_of_it_moved():
    # Line 1 relabels the L-1 boxes to L-2; line 2 picked an L-2 row, and its group now
    # holds them too: one row changed twice is put back to what it was first.
    db = _db([_row(10), _row(20, lot="L-2")])
    before = _cold(db)
    out = _create(db, _payload(_line(10, 1, new="L-2"), _line(20, 2, old="L-2", new="L-3")))
    assert out["status"] == "success" and _lots(db) == {10: "L-3", 20: "L-3"}
    _delete(db)
    assert _cold(db) == before


@pytest.mark.parametrize("change", ["lot", "cartons", "gone"])
def test_the_undo_is_refused_when_a_row_has_changed_since(change):
    db = _db([_row(10), _row(11)])
    _create(db, _payload(_line(10, 1)))
    row = next(r for r in db.tables["cfpl_cold_stocks"] if r["id"] == 10)
    if change == "lot":
        row["lot_no"] = "L-7"                     # relabelled again outside this challan
    elif change == "cartons":
        row["no_of_cartons"] = Decimal("0")
    else:
        db.tables["cfpl_cold_stocks"].remove(row)
    snapshot = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _delete(db)
    assert (exc.value.status_code, exc.value.detail) == (
        409, f"Inner cold transfer {CHALLAN} cannot be undone: 1 of the stock rows it changed have been changed "
             "again since (or have gone). Nothing was changed or deleted.")
    assert db.tables == snapshot


def test_the_undo_is_refused_when_a_box_has_gone_on_an_inter_unit_transfer_since(r3):
    # Record only: a dispatch leaves the cold row as it is, so the row looks unchanged; the
    # box rule says the box is no longer here, and its transfer carries the new lot.
    db = _db([_row(10), _row(11), _row(12, box="", cartons=2)])
    _create(db, _payload(_line(10, 3)))
    r3.calls.clear()
    r3.held.add(("34730011-1", "TR-2026-0001"))
    snapshot = copy.deepcopy(db.tables)
    with pytest.raises(HTTPException) as exc:
        _delete(db)
    assert (exc.value.status_code, exc.value.detail) == (
        409, f"Inner cold transfer {CHALLAN} cannot be undone: 1 of its boxes have gone on an inter-unit transfer "
             "since (box 34730011-1, challan TRANS-1), so they are not at Savla D-39 to be put back. Nothing was "
             "changed or deleted.")
    assert db.tables == snapshot
    (call,) = r3.calls
    assert call[1] == "Savla D-39" and sorted(call[0]) == [("34730010-1", "TR-2026-0001"),
                                                           ("34730011-1", "TR-2026-0001")]
    r3.held.clear()
    assert _delete(db)["success"] is True and _lots(db) == {10: "L-1", 11: "L-1", 12: "L-1"}


def test_a_later_inner_cold_transfer_of_the_row_is_named_and_undone_first():
    db = _db([_row(10)])
    _create(db, _payload(_line(10, 1, new="L-2"), challan="ICT-A"))
    _create(db, _payload(_line(10, 1, old="L-2", new="L-3"), challan="ICT-B"))
    with pytest.raises(HTTPException) as exc:
        _delete(db, "ICT-A")
    assert exc.value.status_code == 409
    assert exc.value.detail == ("Inner cold transfer ICT-A cannot be undone: 1 of the stock rows it changed have "
                                "been changed again since by inner cold transfer ICT-B, which has to be deleted "
                                "first. Nothing was changed or deleted.")
    _delete(db, "ICT-B")
    assert _lots(db) == {10: "L-2"}
    _delete(db, "ICT-A")
    assert _lots(db) == {10: "L-1"} and _written(db) == []


@pytest.mark.parametrize("changes", [True, False], ids=["recorded-elsewhere", "no-change-table"])
def test_a_challan_saved_before_its_changes_were_recorded_is_not_deleted(changes):
    legacy = {"id": 5, "challan_no": "ICT-OLD", "transfer_date": "01-10-2026", "from_warehouse": "Savla D-39",
              "stock_record_id": 10, "quantity": 1, "old_lot_number": "L-1", "new_lot_number": "L-2",
              "created_at": datetime(2026, 10, 1)}
    db = _db([_row(10, lot="L-2")], lines=[legacy], changes=changes)
    with pytest.raises(HTTPException) as exc:
        _delete(db, "ICT-OLD")
    assert exc.value.status_code == 409
    assert exc.value.detail == ("Inner cold transfer ICT-OLD cannot be undone: it was saved before the ERP kept a "
                                "record of the stock rows an inner cold transfer changes, so it is not deleted "
                                "and its stock rows stay as they are.")
    assert db.tables["inner_cold_transfer"] == [legacy] and _lots(db) == {10: "L-2"}


def test_the_undo_needs_the_store_to_be_the_users_and_the_challan_to_exist():
    db = _db([_row(10)])
    _create(db, _payload(_line(10, 1)))
    with pytest.raises(AuthError) as exc:
        _delete(db, user=RISHI)
    assert exc.value.status_code == 403 and _lots(db) == {10: "L-2"}
    with pytest.raises(HTTPException) as exc:
        _delete(db, "ICT-NONE")
    assert exc.value.status_code == 404
    assert _delete(db, user=SAVLA)["success"] is True and _lots(db) == {10: "L-1"}


# ── The list and the read, scoped ────────────────────────────────────────────
class _ReadConn:
    """The list / read statements of inner_cold_service (GROUP BY, MIN), answered from
    `lines` (inner_cold_transfer rows), the scope clause evaluated as Postgres would
    (both sides normalised: sites.site_key)."""

    _SCOPE = WAREHOUSE_NORM_ANY_SQL("from_warehouse", "$1")

    def __init__(self, *lines):
        self.lines = [dict(l) for l in lines]
        self.calls: list = []

    def _visible(self, sql, args):
        if self._SCOPE not in sql:
            return self.lines, args
        keys = set(args[0])
        return [l for l in self.lines if site_key(l["from_warehouse"]) in keys], args[1:]

    async def fetchval(self, sql, *args):
        self.calls.append(sql)
        if "to_regclass" in sql:
            return "inner_cold_transfer"
        lines, _ = self._visible(sql, args)
        return len({l["challan_no"] for l in lines})

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if "GROUP BY challan_no" in sql:
            lines, _ = self._visible(sql, args)
            out = {}
            for l in lines:
                out.setdefault(l["challan_no"], {**l, "line_count": 0, "total_boxes": 0})
                out[l["challan_no"]]["line_count"] += 1
                out[l["challan_no"]]["total_boxes"] += l["quantity"]
            return list(out.values())
        return [l for l in self.lines if l["challan_no"] == args[0]]

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        rows = [l for l in self.lines if l["challan_no"] == args[0]]
        return rows[0] if rows else None


def _audit(challan, store, **over):
    row = {"id": 1, "challan_no": challan, "transfer_date": "16-10-2026", "from_warehouse": store,
           "reason_code": "Other", "remark": "R", "status": "COMPLETED", "created_at": datetime(2026, 10, 16),
           "stock_record_id": 10, "item_category": "NUTS", "item_description": "ALMOND 25KG",
           "net_weight_kg": Decimal("25.000"), "quantity": 1, "old_lot_number": "L-1", "new_lot_number": "L-2",
           "new_storage_location": None}
    row.update(over)
    return row


def test_the_list_is_scoped_to_every_spelling_of_the_users_sites():
    conn = _ReadConn(_audit("ICT-1", "Savla D-39"), _audit("ICT-2", "savla d39"), _audit("ICT-3", "Rishi"),
                     _audit("ICT-4", "Savla Bond"))
    out = asyncio.run(svc.list_inner_cold(conn, page=1, per_page=15, scope=["D-39"]))
    assert {r["challan_no"] for r in out["records"]} == {"ICT-1", "ICT-2"} and out["total"] == 2
    assert any(f"WHERE {WAREHOUSE_NORM_ANY_SQL('from_warehouse', '$1')}" in s for s in conn.calls)
    out = asyncio.run(svc.list_inner_cold(conn, page=1, per_page=15, scope=["Savla Bond"]))
    assert {r["challan_no"] for r in out["records"]} == {"ICT-4"}       # a store of its own (D3)
    assert asyncio.run(svc.list_inner_cold(conn, page=1, per_page=15, scope=[]))["total"] == 4
    assert scope_keys(["D-39"]) and site_key("savla d39") in scope_keys(["D-39"])


@pytest.mark.parametrize("scope, visible", [([], True), (["D-39"], True), (["Savla D-39 Cold"], True),
                                            (["Rishi"], False), (["Savla Bond"], False)])
def test_the_read_of_a_challan_outside_the_users_sites_is_404(scope, visible):
    conn = _ReadConn(_audit("ICT-1", "Savla D-39"))
    if visible:
        assert asyncio.run(svc.get_inner_transfer(conn, "ICT-1", scope=scope))["challan_no"] == "ICT-1"
        return
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.get_inner_transfer(conn, "ICT-1", scope=scope))
    assert (exc.value.status_code, exc.value.detail) == (404, "Inner cold transfer not found")


# ── Migration 124 (static; the SQL is applied by the user, not by tests) ─────
ROOT = Path(__file__).resolve().parents[2]
SQL_124 = ROOT / "app" / "db" / "124_inner_cold_transfer_changes.sql"


def _statements(sql: str) -> str:
    return re.sub(r"\s+", " ", "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")))


def test_124_creates_the_change_table_the_service_writes():
    body = _statements(SQL_124.read_text(encoding="utf-8"))
    start = body.index("CREATE TABLE IF NOT EXISTS public.inner_cold_transfer_changes (")
    table = body[start:body.index(");", start)]
    insert = re.search(r"INSERT INTO inner_cold_transfer_changes\s*\(([^)]*)\)",
                       Path(svc.__file__).read_text(encoding="utf-8"))
    written = [c.strip() for c in insert.group(1).split(",")]
    assert len(written) == 17
    for column in written:
        assert re.search(rf"\b{column}\b", table), column
    assert "CHECK (cold_table IN ('cfpl_cold_stocks', 'cdpl_cold_stocks'))" in table
    assert "CHECK (kind IN ('update', 'insert'))" in table
    # The audit table too, when IMS has not made it, with every column the create writes.
    audit = body[body.index("CREATE TABLE IF NOT EXISTS public.inner_cold_transfer ("):start]
    for column in ("challan_no", "transfer_date", "from_warehouse", "reason_code", "remark", "stock_record_id",
                   "item_category", "item_description", "net_weight_kg", "quantity", "old_lot_number",
                   "new_lot_number", "new_storage_location", "old_storage_location", "transfer_type"):
        assert re.search(rf"\b{column}\b", audit), column


def test_124_is_one_idempotent_transaction_registered_after_123():
    raw = SQL_124.read_bytes()
    body = _statements(raw.decode("utf-8"))
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1 and "ROLLBACK" not in body
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
    creates = re.findall(r"CREATE (?:TABLE|INDEX) (\S+ \S+ \S+)", body)
    assert creates and all(c == "IF NOT EXISTS public.inner_cold_transfer" or c.startswith("IF NOT EXISTS")
                           for c in creates)
    assert all("IF NOT EXISTS" in a for a in re.findall(r"ALTER TABLE [^;]+;", body))
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i123 = text.index('DB_DIR / "123_interunit_transfer_boxes_box_index.sql"')
    i124 = text.index('DB_DIR / "124_inner_cold_transfer_changes.sql"')
    assert i123 < i124 < text.index("# ── Optional: drop the v1 legacy")
