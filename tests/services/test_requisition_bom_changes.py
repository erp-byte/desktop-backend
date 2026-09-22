"""Floor requisitions and receive-material honour the job card's BOM changes (spec 2f, 2g)."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.modules.floor_requisition.services import requisition_service as svc
from app.modules.production import router as PR
from app.modules.production.services import jc_bom_changes as m

COLS = [c.strip() for c in svc.COLS.split(",")]
INSERT_COLS = ["requisition_id", "job_card_id", "warehouse", "floor", "material_sku_name",
               "item_type", "requested_qty", "requested_unit", "required_qty", "required_unit",
               "available_qty", "available_unit", "shortage_qty", "shortage_unit",
               "note", "raised_by"]
TAG = re.compile(r"/\* (jcbc:[a-z_]+) \*/")


def _change(change_type, name, item_type="rm", required_qty=None):
    return {"change_id": 5, "change_type": change_type, "material_sku_name": name, "item_type": item_type,
            "sku_id": 77, "required_qty": required_qty,
            "required_unit": None if required_qty is None else ("kg" if item_type == "rm" else "pcs")}


# ── the two helpers ──
class HelperConn:
    def __init__(self, table=True):
        self.table, self.calls = table, []

    async def fetchval(self, sql, *a):
        tag = TAG.search(sql).group(1)
        self.calls.append((tag, a))
        return {"jcbc:table": self.table, "jcbc:head": 900}[tag]

    async def fetchrow(self, sql, *a):
        tag = TAG.search(sql).group(1)
        self.calls.append((tag, a))
        if tag == "jcbc:card":
            return {"job_card_id": 901, "job_card_number": "PLAN-7-L1-S2", "plan_line_id": 70,
                    "bom_id": 9, "status": "in_progress"}
        assert tag == "jcbc:live_change"
        return {"change_id": 5, "change_type": "added", "material_sku_name": "Sugar", "item_type": "rm",
                "sku_id": 77, "required_qty": Decimal("2.500"), "required_unit": "kg"}

    async def fetch(self, sql, *a):
        tag = TAG.search(sql).group(1)
        self.calls.append((tag, a))
        return {"jcbc:chain": [], "jcbc:removed_keys": [{"k": "OLD SALT"}]}[tag]


def test_live_change_for_reads_the_chains_change_by_article():
    conn = HelperConn()
    got = asyncio.run(m.live_change_for(conn, 901, " sugar "))
    assert got["change_type"] == "added" and got["required_qty"] == 2.5
    assert ("jcbc:live_change", (900, "SUGAR")) in conn.calls


def test_removed_keys_for_is_the_chains_removed_articles():
    conn = HelperConn()
    assert asyncio.run(m.removed_keys_for(conn, 901)) == {"OLD SALT"}
    assert ("jcbc:removed_keys", (900,)) in conn.calls


def test_before_115_the_helpers_find_nothing():
    conn = HelperConn(table=False)
    assert asyncio.run(m.live_change_for(conn, 901, "Sugar")) is None
    assert asyncio.run(m.removed_keys_for(conn, 901)) == set()
    assert [t for t, _ in conn.calls] == ["jcbc:table", "jcbc:table"]
    assert asyncio.run(m.live_change_for(HelperConn(), 901, "  ")) is None


# ── raise_requisition ──
class ReqConn:
    def __init__(self, order, *, bom=None, indents=()):
        self.order, self.bom, self.indents = order, bom, list(indents)
        self.calls: list[tuple[str, str, tuple]] = []

    def is_in_transaction(self):
        return True

    def transaction(self):          # insert_with_pk_retry's savepoint
        return _Ctx(self)

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        self.calls.append((kind, s, args))
        return s

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        if s.startswith("SELECT job_card_id, factory, floor, bom_id FROM job_card_v2"):
            self.order.append("card")
            return {"job_card_id": 12345678, "factory": "W-202", "floor": "First Floor", "bom_id": 9}
        if "FROM bom_line" in s:
            self.order.append("bom")
            return self.bom
        if s.startswith("INSERT INTO floor_requisition"):
            self.order.append("insert")
            row = dict.fromkeys(COLS)
            row.update(zip(INSERT_COLS, args))
            row.update(status="raised")
            return row
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if "job_card_rm_indent_v2" in s:
            self.order.append("indents")
            return self.indents
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        raise AssertionError(f"unexpected fetchval: {s[:90]}")


class User:
    full_name, email, phone, user_id = "Ravi K", "", "", 7
    allowed_warehouses: list = []
    allowed_floors: list = []


@pytest.fixture
def patched(monkeypatch):
    """Scripted plan-line lock, live change and floor stock; `order` records the calls."""
    state = {"order": [], "change": None, "stock": [], "change_args": None}

    async def lock(conn, job_card_id):
        state["order"].append("lock")
        return 70

    async def live_change(conn, job_card_id, name):
        state["order"].append("change")
        state["change_args"] = (job_card_id, name)
        return state["change"]

    async def floor_stock(conn, *, warehouse, floor):
        return {"warehouse": warehouse, "floor": floor, "items": state["stock"]}

    monkeypatch.setattr(m, "lock_line_for_card", lock)
    monkeypatch.setattr(m, "live_change_for", live_change)
    monkeypatch.setattr(svc.floor_stock_service, "fetch_floor_stock", floor_stock)
    return state


def _raise(conn, **kw):
    kw.setdefault("job_card_id", 12345678)
    kw.setdefault("note", None)
    return asyncio.run(svc.raise_requisition(conn, User(), **kw))


def test_a_removed_article_is_refused_and_nothing_is_inserted(patched):
    patched["change"] = _change("removed", "Old Salt")
    conn = ReqConn(patched["order"], bom={"material_sku_name": "Old Salt", "item_type": "rm"})
    with pytest.raises(svc.RequisitionError) as exc:
        _raise(conn, material_sku_name=" old salt ", requested_qty="2")
    assert (exc.value.status, exc.value.error) == (422, "article_removed_from_job_card")
    assert "insert" not in patched["order"]
    assert not [c for c in conn.calls if c[1].startswith("INSERT")]


def test_an_added_rm_article_is_raised_in_kg_against_its_required_qty(patched):
    patched["change"] = _change("added", "Sugar", "rm", 2.5)
    patched["stock"] = [{"item_name": "SUGAR ", "stock_type": "Fresh Stock", "available_kg": 1.0,
                         "available_quantity": 0},
                        {"item_name": "Salt", "stock_type": "Fresh Stock", "available_kg": 50,
                         "available_quantity": 0}]
    conn = ReqConn(patched["order"], bom=None, indents=[])
    out = _raise(conn, material_sku_name=" sugar ", requested_qty="1.5")
    assert out["material_sku_name"] == "Sugar" and out["item_type"] == "RM"
    assert (out["requested_qty"], out["requested_unit"]) == (Decimal("1.500"), "kg")
    assert (out["required_qty"], out["required_unit"]) == (Decimal("2.500"), "kg")
    assert (out["available_qty"], out["shortage_qty"]) == (Decimal("1.000"), Decimal("1.500"))
    assert patched["change_args"] == (12345678, " sugar ")


def test_an_added_pm_article_without_a_required_qty_is_in_pieces_with_no_shortage(patched):
    patched["change"] = _change("added", "Tape", "pm", None)
    patched["stock"] = [{"item_name": "Tape", "stock_type": "Fresh Stock", "available_kg": 0,
                         "available_quantity": 40}]
    conn = ReqConn(patched["order"], bom=None, indents=[])
    out = _raise(conn, material_sku_name="Tape", requested_qty="10")
    assert out["item_type"] == "PM" and out["requested_unit"] == "pcs"
    assert out["required_qty"] is None and out["required_unit"] is None
    assert out["shortage_qty"] is None and out["shortage_unit"] is None
    assert (out["available_qty"], out["available_unit"]) == (Decimal("40.000"), "pcs")


def test_the_plan_line_lock_comes_before_the_bom_lookup(patched):
    conn = ReqConn(patched["order"], bom={"material_sku_name": "Seeds", "item_type": "rm"})
    _raise(conn, material_sku_name="Seeds", requested_qty="3")
    order = patched["order"]
    assert order.index("card") < order.index("lock") < order.index("change") < order.index("bom")


# ── receive-material ──
class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *exc):
        return False


BOXES = {"BX1": {"box_id": "BX1", "material_sku_name": "Old Salt", "qty_kg": 5},
         "BX2": {"box_id": "BX2", "material_sku_name": "Seeds", "qty_kg": 10}}
INDENTS = {"Old Salt": {"rm_indent_id": 11, "scanned_box_ids": [], "material_sku_name": "Old Salt"},
           "Seeds": {"rm_indent_id": 12, "scanned_box_ids": [], "material_sku_name": "Seeds"}}


class RecvConn:
    def __init__(self, order):
        self.order, self.executed = order, []

    def transaction(self):
        return _Ctx(self)

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT status FROM job_card_v2"):
            self.order.append("card")
            return {"status": "assigned"}
        if "FROM po_box" in s:
            self.order.append("box")
            return BOXES.get(args[0])
        if "FROM job_card_rm_indent_v2" in s:
            row = INDENTS.get(args[1])
            if row is None:
                return None
            # Only the columns the statement selects, as Postgres would return them.
            cols = s.split("SELECT ", 1)[1].split(" FROM", 1)[0]
            return {k: v for k, v in row.items() if k in [c.strip() for c in cols.split(",")]}
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "UPDATE 1"


def test_receive_refuses_a_removed_articles_box_and_attaches_the_rest(monkeypatch):
    order: list[str] = []

    async def lock(conn, job_card_id):
        order.append("lock")
        return 70

    async def removed(conn, job_card_id):
        order.append("removed_keys")
        return {"OLD SALT"}

    monkeypatch.setattr(m, "lock_line_for_card", lock)
    monkeypatch.setattr(m, "removed_keys_for", removed)
    conn = RecvConn(order)
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    out = asyncio.run(PR.receive_material_v2(req, 3, PR.ReceiveMaterialV2Request(box_ids=["BX1", "BX2"]),
                                             user=SimpleNamespace(full_name="Op")))

    assert out["attached"][0] == {"box_id": "BX1", "error": "article_removed_from_job_card",
                                  "material_sku_name": "Old Salt"}
    assert out["attached"][1] == {"box_id": "BX2", "status": "attached", "rm_indent_id": 12}
    indent_updates = [a for s, a in conn.executed if s.startswith("UPDATE job_card_rm_indent_v2")]
    assert len(indent_updates) == 1 and indent_updates[0][2] == 12
    assert order.index("card") < order.index("lock") < order.index("removed_keys") < order.index("box")
