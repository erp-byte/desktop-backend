"""Site names outside the transfer screens compare as sites (the one site table,
app.core.sites): any spelling of a site is that site, and 'Cold Storage' and 'Savla
Bond' are sites of their own (D2, D3). The SQL the services write is evaluated by the
WHERE translator of test_cold_pick_and_search_r3.

What is pinned:
  - P1-16: a role's warehouse scope (check_permission) matches ?warehouse= by site —
    'W-202' covers 'W202', 'D-39' covers 'Savla D-39' — and still refuses another site;
  - P1-26: the customer-return cold guard is the site table's cold kind, in any
    spelling; the warehouse owner's CC is routed by site, and every CC the old rule
    sent is still sent;
  - P1-29: the lot picker's warehouse filter and its other-warehouses exclusion match
    every spelling of the site, a batch with no warehouse recorded is still an "other
    warehouse", and is_cold_storage is the cold kind (or a cold floor);
  - P1-35: the v1 job-card list's factory lock and filter compare sites; the notify-qc
    recipients match a role's warehouse scope by site;
  - P1-09: the ledger names its godowns from the site table and groups exactly as
    before; where it differs from the table is pinned (as reported to the lead);
  - the pending-stock Swagger text and docs/interunit-transfer-flow.md describe the
    record-only design.

Run:  PYTHONPATH=. python -m pytest tests/services/test_site_alias_other_modules.py
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import openapi_tags
from app.core.sites import canonical_site, scope_keys, site_aliases, to_sites
from app.core.warehouse_scope import WAREHOUSE_NORM_SQL
from app.modules.auth.services import permission_service
from app.modules.customer_returns.services import box_service, mail_service
from app.modules.ledger.services import godown_alias
from app.modules.production import router as production_router
from app.modules.production.services import lot_issuance_service, qc_notify
from tests.services.test_cold_pick_and_search_r3 import _norm, _predicate

ROOT = Path(__file__).resolve().parents[2]


def _ws(s: str) -> str:
    return " ".join(s.split())


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquired:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquired()


def _request(conn):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=_Pool(conn))))


# ── P1-16: a role's warehouse scope ──────────────────────────────────────────
class _PermConn:
    """The same auth_role_permission row at whatever level is asked."""

    def __init__(self, scope):
        self.row = {"allowed_entities": None, "allowed_warehouses": scope, "allowed_floors": None}

    async def fetch(self, sql, *args):
        return [dict(self.row)]


@pytest.mark.parametrize("scope, warehouse, ok", [
    (["W-202"], "W202", True), (["A185"], "A-185", True), (["A185"], "Warehouse A185", True),
    (["D-39"], "Savla D-39", True), (["Supreme"], "Supreme Cold", True), (["Savla Bond"], "savla-bond", True),
    (["A185"], "W202", False), (["Savla D-39"], "Savla Bond", False), (["Rishi"], "Cold Storage", False),
    (["W202"], None, True), (None, "A185", True), ([], "A185", True),
])
def test_a_roles_warehouse_scope_compares_sites(scope, warehouse, ok):
    granted = asyncio.run(permission_service.check_permission(
        _PermConn(scope), [7], False, "production", "job_cards", None, "view", warehouse=warehouse))
    assert granted is ok


# ── P1-26: customer returns ──────────────────────────────────────────────────
@pytest.mark.parametrize("unit", [
    "D-39", "D-514", "Savla D-39", "savla d514", "Old Savla", "New Savla", "Savla Bond", "Rishi", "Rishi Cold",
    "Rishi Cold Storage Pvt Ltd", "Supreme", "Supreme Cold", "Supreme Cold Storage", "ESKIMO", "Eskimo Cold",
    "Savla D-34",
    "Cold Storage",          # cold in kind (D2), though it is no one store
])
def test_a_cold_store_return_is_cold_in_any_spelling(unit):
    assert box_service._is_cold_warehouse(unit)


@pytest.mark.parametrize("unit", [None, "", "W202", "W-202", "A-185", "A185 Cold", "Warehouse A185", "F53", "Savla",
                                  "Dev Int"])
def test_a_warehouse_return_is_not_cold(unit):
    assert not box_service._is_cold_warehouse(unit)


class _CRConn:
    """The header row, and cfpl_cold_stocks holding `ims_rows` rows IMS made from it
    (no cr_cold_stock_rows on this database)."""

    def __init__(self, factory_unit, ims_rows=0):
        self.factory_unit = factory_unit
        self.ims_rows = ims_rows

    async def fetchrow(self, sql, *args):
        return {"id": 5, "factory_unit": self.factory_unit}

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return args[0] == "cfpl_cold_stocks"
        assert sql.startswith("SELECT COUNT(*) FROM cfpl_cold_stocks"), sql
        return self.ims_rows


def test_box_entry_for_a_cold_store_return_ims_put_in_cold_stock_is_refused_whatever_its_spelling():
    tables = {"header": "cfpl_rtv_header"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(box_service._resolve_box_writable_header(_CRConn("Supreme Cold", ims_rows=2), tables, "CR-1"))
    assert exc.value.status_code == 409 and exc.value.detail["error"] == "cold_stock_from_ims"
    assert asyncio.run(box_service._resolve_box_writable_header(
        _CRConn("Supreme Cold"), tables, "CR-1")) == (5, "Supreme Cold")
    assert asyncio.run(box_service._resolve_box_writable_header(
        _CRConn("A-185", ims_rows=2), tables, "CR-1")) == (5, "A-185")


# The warehouse-owner rows 077 / 078 seed, in fetch_routing's order.
ROUTING = {"warehouse_cc": [
    {"match_key": k, "match_type": t, "email": e} for k, t, e in (
        ("savla", "substring", "vaibhav.kumkar@candorfoods.in"), ("rishi", "substring", "vaibhav.kumkar@candorfoods.in"),
        ("supreme", "substring", "vaibhav.kumkar@candorfoods.in"), ("eskimo", "substring", "vaibhav.kumkar@candorfoods.in"),
        ("w202", "code", "vaibhav.kumkar@candorfoods.in"), ("a185", "code", "stores-a185@candorfoods.in"),
        ("a68", "code", "pankaj.ranga@candorfoods.in"), ("d39", "code", "vaibhav.kumkar@candorfoods.in"),
        ("d514", "code", "vaibhav.kumkar@candorfoods.in"))]}


def _old_cc(routing, factory_unit):
    """The rule before the site table: raw substring, or the code with '-' and ' ' dropped."""
    if not factory_unit:
        return None
    low = str(factory_unit).strip().lower()
    code = low.replace("-", "").replace(" ", "")
    for r in routing.get("warehouse_cc", []):
        key = (r.get("match_key") or "").strip().lower()
        if not key:
            continue
        if r.get("match_type") == "substring" and key in low:
            return r["email"]
        if r.get("match_type") == "code" and code == key.replace("-", "").replace(" ", ""):
            return r["email"]
    return None


@pytest.mark.parametrize("unit, email", [
    ("Warehouse A185", "stores-a185@candorfoods.in"), ("A-185", "stores-a185@candorfoods.in"),
    ("A185 Cold", "stores-a185@candorfoods.in"), ("W-202", "vaibhav.kumkar@candorfoods.in"),
    ("Warehouse W202", "vaibhav.kumkar@candorfoods.in"), ("D-39", "vaibhav.kumkar@candorfoods.in"),
    ("Supreme Cold Storage", "vaibhav.kumkar@candorfoods.in"), ("A68", "pankaj.ranga@candorfoods.in"),
    ("F53", None), ("Cold Storage", None), ("Dev Int", None), (None, None), ("", None),
])
def test_the_warehouse_owner_cc_is_routed_by_site(unit, email):
    assert mail_service._warehouse_cc_email(ROUTING, unit) == email


def test_a_substring_row_also_matches_the_sites_canonical_name():
    only_savla = {"warehouse_cc": [{"match_key": "savla", "match_type": "substring", "email": "cold@x"}]}
    assert mail_service._warehouse_cc_email(only_savla, "D-514") == "cold@x"     # D-514 is Savla D-514
    assert _old_cc(only_savla, "D-514") is None


def test_every_cc_the_old_rule_sent_is_still_sent():
    spellings = {s for site in (*to_sites(), "Cold Storage", "Dev Int") for s in site_aliases(site)}
    spellings |= {"A-185", "W-202", "d 39", "savla-d514", "Savla", "SAVLA BOND", "apmc-f53", "some shed"}
    for raw in sorted(spellings):
        old = _old_cc(ROUTING, raw)
        if old is not None:
            assert mail_service._warehouse_cc_email(ROUTING, raw) == old, raw


# ── P1-29: the production lot picker ─────────────────────────────────────────
def _batch(batch_id, warehouse_id, floor_id="rm_store", status="AVAILABLE"):
    return {"batch_id": batch_id, "sku_name": "Cashew 320", "current_qty_kg": 50, "status": status,
            "entity": "cfpl", "warehouse_id": warehouse_id, "floor_id": floor_id}


BATCHES = [
    _batch("B-W202", "W202"), _batch("B-W-202", "W-202"), _batch("B-WH", "Warehouse W202", "pm_store"),
    _batch("B-A185", "A185"), _batch("B-A185C", "A185 Cold"), _batch("B-RISHI", "Rishi", "cold_store"),
    _batch("B-SUP", "Supreme"), _batch("B-D34", "Savla D-34", None), _batch("B-NONE", None, None),
    _batch("B-HOLD", "W202", status="QC_HOLD"), _batch("B-COLDFLOOR", "W202", "cold room 2"),
]


class _BatchConn:
    """inventory_batch; the WHERE the service writes is evaluated (the lot_block join
    finds no block)."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        s = _ws(sql)
        self.calls.append((s, args))
        keep = _predicate(s.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0])
        return [{**b, "item_description": b["sku_name"], "warehouse": b["warehouse_id"], "expiry_date": None,
                 "blocked_for_so": None} for b in BATCHES if keep(b, args)]


def _lots(fn, **kw) -> dict:
    out = asyncio.run(fn(_BatchConn(), item_description="cashew", entity="cfpl", **kw))
    return {lot["batch_id"]: lot["is_cold_storage"] for lot in out["lots"]}


def test_the_lot_picker_finds_a_warehouses_batches_in_any_spelling():
    assert set(_lots(lot_issuance_service.get_lots, warehouse="W-202")) == {"B-W202", "B-W-202", "B-WH", "B-COLDFLOOR"}
    assert set(_lots(lot_issuance_service.get_lots, warehouse="Warehouse A185")) == {"B-A185", "B-A185C"}
    # The floor / warehouse text still matches as before.
    assert set(_lots(lot_issuance_service.get_lots, warehouse="cold_store")) == {"B-RISHI"}


def test_other_warehouses_leave_out_every_spelling_and_keep_a_batch_with_no_warehouse():
    assert set(_lots(lot_issuance_service.get_lots_other_warehouses, exclude_warehouse="W-202")) == {
        "B-A185", "B-A185C", "B-RISHI", "B-SUP", "B-D34", "B-NONE"}
    assert "B-RISHI" not in _lots(lot_issuance_service.get_lots_other_warehouses, exclude_warehouse="Rishi Cold")


def test_is_cold_storage_is_the_cold_kind_or_a_cold_floor():
    flags = _lots(lot_issuance_service.get_lots)
    assert {b for b, cold in flags.items() if cold} == {"B-RISHI", "B-SUP", "B-D34", "B-COLDFLOOR"}
    assert flags["B-A185C"] is False                      # 'A185 Cold' is a floor of A185 (D5)
    assert _lots(lot_issuance_service.get_lots_other_warehouses)["B-SUP"] is True


# ── P1-35: v1 job cards and the notify-qc recipients ─────────────────────────
CARDS = [{"job_card_id": n, "factory": f, "deleted_at": None}
         for n, f in enumerate(("A-185", "A185", "Warehouse A185", "W-202", "Savla D-39"), 1)]


class _CardConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def _hits(self, s, args):
        keep = _predicate(s.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0])
        return [c for c in CARDS if keep(c, args)]

    async def fetchval(self, sql, *args):
        s = _ws(sql)
        self.calls.append((s, args))
        return len(self._hits(s, args))

    async def fetch(self, sql, *args):
        s = _ws(sql)
        self.calls.append((s, args))
        return [] if s.startswith("SELECT DISTINCT") else self._hits(s, args)


def _cards(user, factory=None) -> list[int]:
    out = asyncio.run(production_router.list_job_cards(
        _request(_CardConn()), entity=None, status=None, team_leader=None, floor=None, factory=factory, stage=None,
        search=None, customer=None, article=None, date_from=None, date_to=None, page=1, page_size=200, size=None,
        include_cancelled=False, user=user))
    return [c["job_card_id"] for c in out["results"]]


def _user(*warehouses, admin=False):
    return SimpleNamespace(allowed_floors=[], allowed_warehouses=list(warehouses), is_admin=admin)


def test_the_v1_job_card_lock_and_filter_compare_factories_as_sites():
    assert _cards(_user("A185")) == [1, 2, 3]
    assert _cards(_user("D-39")) == [5]
    assert _cards(_user("A185"), factory="A-185") == [1, 2, 3]     # was 403: 'A-185' not in ['A185']
    assert _cards(_user(admin=True), factory="W202") == [4]
    with pytest.raises(HTTPException) as exc:
        _cards(_user("A185"), factory="W202")
    assert exc.value.status_code == 403


class _QCConn:
    def __init__(self, factory):
        self.factory = factory
        self.recipient_calls: list[tuple[str, tuple]] = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False
        return _Tx()

    async def fetchrow(self, sql, *args):
        return {"status": "completed", "entity": "cfpl", "factory": self.factory, "floor": "floor-1"}

    async def fetch(self, sql, *args):
        self.recipient_calls.append((_ws(sql), args))
        return []


def test_the_qc_recipients_match_a_roles_warehouse_scope_by_site(monkeypatch):
    async def _dispatch(conn, **kw):
        return {"notification_id": 1}
    monkeypatch.setattr(qc_notify, "dispatch", _dispatch)
    conn = _QCConn("W-202")
    out = asyncio.run(production_router.notify_qc_v2(_request(conn), 5, None, user=SimpleNamespace(user_id=9)))
    assert out["warning"] == "no_qc_recipients_in_scope"
    ((sql, args),) = conn.recipient_calls
    assert _ws(f"OR EXISTS (SELECT 1 FROM unnest(rp.allowed_warehouses) AS aw "
               f"WHERE {WAREHOUSE_NORM_SQL('aw')} = ANY($2::text[])))") in sql
    assert "$2 = ANY(rp.allowed_warehouses)" not in sql
    assert args == ("cfpl", scope_keys("W-202"), "floor-1")
    # What that clause decides, per role scope (WAREHOUSE_NORM_SQL is _norm):
    scopes = (["W202"], ["Warehouse W202"], ["w-202", "A185"], ["A185"], ["Cold Storage"])
    assert [any(_norm(w) in args[1] for w in s) for s in scopes] == [True, True, True, False, False]


# ── P1-09: the ledger's godowns ──────────────────────────────────────────────
# GODOWN_ALIASES as it was before the site table: the ledger's grouping must not move.
OLD_GODOWNS = {
    "savla d-39": "Savla D-39", "savla d39": "Savla D-39", "savla-d39": "Savla D-39", "savla-d-39": "Savla D-39",
    "d-39": "Savla D-39", "d39": "Savla D-39", "old savla": "Savla D-39", "savla d-39 cold": "Savla D-39",
    "savla d39 cold": "Savla D-39", "savla": "Savla D-39",
    "savla d-514": "Savla D-514", "savla d514": "Savla D-514", "savla-d514": "Savla D-514",
    "savla-d-514": "Savla D-514", "d-514": "Savla D-514", "d514": "Savla D-514", "new savla": "Savla D-514",
    "savla d-514 cold": "Savla D-514", "savla d514 cold": "Savla D-514",
    "savla bond": "Savla Bond",
    "rishi": "Rishi", "rishi cold": "Rishi", "rishi cold storage": "Rishi", "rishi cold storage pvt ltd": "Rishi",
    "supreme": "Supreme", "supreme cold": "Supreme", "supreme cold storage": "Supreme",
    "eskimo": "Eskimo", "eskimo cold": "Eskimo", "eskimo cold storage": "Eskimo",
    "w202": "W202", "warehouse w202": "W202", "a101": "A101", "warehouse a101": "A101",
    "a185": "A185", "warehouse a185": "A185", "a-185": "A185", "a-185 cold": "A185",
    "a68": "A68", "warehouse a68": "A68", "f53": "F53", "warehouse f53": "F53", "dev int": "Dev Int",
}


def test_the_ledger_groups_every_spelling_as_before():
    assert godown_alias.GODOWN_ALIASES == OLD_GODOWNS


def test_each_ledger_godown_is_the_site_tables_name_for_its_spelling():
    for spelling, godown in godown_alias.GODOWN_ALIASES.items():
        if spelling not in godown_alias.AMBIGUOUS_ALIASES:
            assert canonical_site(spelling) == godown, spelling
    assert godown_alias.AMBIGUOUS_ALIASES == {"savla"}


@pytest.mark.parametrize("raw, ledger, table", [
    ("savla", "Savla D-39", "savla"),                     # the ledger's own guess; no site in the table
    ("Savla D-34", "Savla D-34", "Savla D-39"),           # the table's alias (D5), not grouped by the ledger
    ("A185 Cold", "A185 Cold", "A185"),
    ("APMC F53", "Apmc F53", "F53"),
    ("Savla D-39 Cold Storage", "Savla D-39 Cold Storage", "Savla D-39"),
    ("Savla D-514 Cold Storage", "Savla D-514 Cold Storage", "Savla D-514"),
    ("W-202", "W-202", "W202"),                           # punctuation: the ledger's lookup is exact
    ("Savla Bond", "Savla Bond", "Savla Bond"),           # D3: the same in both
])
def test_where_the_ledger_and_the_site_table_differ(raw, ledger, table):
    assert godown_alias.ledger_godown(raw) == ledger
    assert canonical_site(raw) == table


# ── The record-only texts ────────────────────────────────────────────────────
def test_the_pending_stock_swagger_text_is_record_only():
    text = openapi_tags.TAG_DESCRIPTIONS["Transfer - Pending Stock (In Transit)"]
    assert "— deducted from the source" not in text and "not yet posted to the destination" not in text
    assert "Record only" in text and "no stock is deducted from the source or posted to the destination" in text


def test_the_transfer_flow_doc_is_record_only():
    raw = (ROOT / "docs" / "interunit-transfer-flow.md").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    text = raw.decode("utf-8")
    for stale in ("DEDUCT source", "source deducted", "stock posted to destination", "INSERT destination cold_stocks",
                  "un-posts stock", "pending → back to source", "Where stock actually moves"):
        assert stale not in text, stale
    for fact in ("**Record only.**", "COPY each box → pending_transfer_stock",
                 "pick_from_pending: DELETE its pending rows", "Undo paths — transfer records only",
                 "Undo paths touch transfer records only", "Inner-cold is the exception",
                 "moving boxes to another cold store has to be an inter-unit transfer"):
        assert fact in text, fact
    assert re.search(r"never writes the stock tables", text)
