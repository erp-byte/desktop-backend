"""The cold box pick and the cold stock search judge each box by R3 (where its latest
transfer left it), read both companies' cold tables and compare sites, not strings —
against an in-memory fake of the cold tables that EVALUATES the statements
search_service sends (the WHERE clauses it writes are translated to Python; anything
else is an AssertionError, so a change in the SQL's shape fails here).

What is pinned:
  - the pick (P2-02): the lot's rows are read a page at a time in id order, and each
    page's boxes are judged by lookup_service.unavailable_boxes with the dispatch's
    From site and the transfer being edited, until qty boxes pass; without a From site
    a box with any transfer history is skipped, as before; rows without a box identity
    (no box_id or transaction_no, or 'DIRECT') and the boxes in `exclude` are skipped
    and never judged; a box listed twice is returned once, its id trimmed; given the
    picked search row's unit / storage_location only that group's rows are read, so
    another store's rows of the same lot never come back; read only;
  - the search (P2-08): cfpl_cold_stocks AND cdpl_cold_stocks are read, their lots
    merged oldest inward first and the limit applied after the merge, each lot with its
    own company;
  - the search's unit / storage_location filters (P1-28) compare sites
    (app.core.sites): unit on its column; storage_location as the row's store
    (sites.cold_row_site: its unit when that names a cold store, else its location);
    'Savla Bond' and 'Cold Storage' are sites of their own (D3, D2), never a store's
    spelling;
  - available_here (P2-15): with a From site each lot counts the boxes R3 lets go from
    there, in one judgement for every lot found (rows without a box identity not
    counted), the raw counts unchanged; without one it is None and nothing is judged;
  - the two routes pass the new query parameters through.

R3 itself is lookup_service's (test_transfer_box_rules pins it). Here it is a stand-in
that records what it is asked; test_transfer_record_only_edges runs the pick against the
real one.

Run:  PYTHONPATH=. python -m pytest tests/services/test_cold_pick_and_search_r3.py
"""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.sites import same_site
from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.cold_storage import router as cold_router
from app.modules.cold_storage.services import search_service
from app.modules.transfer.services import lookup_service

LOT_COLS = ("item_description", "lot_no", "inward_no", "item_mark", "storage_location", "unit")


# ── The fake cold tables ─────────────────────────────────────────────────────
def _norm(v) -> str:
    """regexp_replace(upper(coalesce(v, '')), '[^A-Z0-9]', '', 'g')"""
    return re.sub(r"[^A-Z0-9]", "", (v or "").upper())


def _text(v):
    """CAST(v AS TEXT)"""
    if v is None:
        return None
    return v.isoformat() if isinstance(v, date) else str(v)


def _ilike(v, pattern) -> bool:
    if v is None or pattern is None:
        return False
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, v, re.I | re.S) is not None


def _lot(r) -> tuple:
    return tuple(r[c] or "" for c in LOT_COLS)


_P = r"\$(\d+)"
_COL = r"(?:\w+\.)?(\w+)"     # a column; its table alias is dropped
_TERMS = [
    (re.compile(r"\(COALESCE\(item_description, ''\), COALESCE\(lot_no, ''\), COALESCE\(inward_no, ''\), "
                r"COALESCE\(item_mark, ''\), COALESCE\(storage_location, ''\), COALESCE\(unit, ''\)\) "
                r"IN \(SELECT \* FROM unnest\(" + ", ".join([_P + r"::text\[\]"] * 6) + r"\)\)"),
     lambda m: f"(_lot(r) in set(zip({', '.join(f'a[{int(g) - 1}]' for g in m.groups())})))"),
    (re.compile(r"regexp_replace\(upper\(coalesce\(" + _COL + r", ''\)\), '\[\^A-Z0-9\]', '', 'g'\) = ANY\( "
                r"SELECT regexp_replace\(upper\(coalesce\(w, ''\)\), '\[\^A-Z0-9\]', '', 'g'\) "
                r"FROM unnest\(" + _P + r"::text\[\]\) AS w\)"),
     lambda m: f"(_norm(r[{m[1]!r}]) in {{_norm(x) for x in a[{int(m[2]) - 1}]}})"),
    (re.compile(r"CAST\(" + _COL + r" AS TEXT\) ILIKE " + _P),
     lambda m: f"_ilike(_text(r[{m[1]!r}]), a[{int(m[2]) - 1}])"),
    (re.compile(r"COALESCE\(" + _COL + r", ''\) ILIKE " + _P),
     lambda m: f"_ilike(r[{m[1]!r}] or '', a[{int(m[2]) - 1}])"),
    (re.compile(_COL + r" ILIKE " + _P), lambda m: f"_ilike(r[{m[1]!r}], a[{int(m[2]) - 1}])"),
    (re.compile(r"COALESCE\(" + _COL + r", ''\) = " + _P),
     lambda m: f"((r[{m[1]!r}] or '') == a[{int(m[2]) - 1}])"),
    (re.compile(r"CAST\(" + _COL + r" AS TEXT\) = " + _P), lambda m: f"(_text(r[{m[1]!r}]) == a[{int(m[2]) - 1}])"),
    (re.compile(_COL + r" NOT IN \(((?:'[^']*',\s*)*'[^']*')\)"),
     lambda m: f"(r[{m[1]!r}] is not None and r[{m[1]!r}] not in ({m[2]},))"),
    (re.compile(_COL + r" IS NULL"), lambda m: f"(r[{m[1]!r}] is None)"),
    (re.compile(_COL + r" > " + _P), lambda m: f"(r[{m[1]!r}] > a[{int(m[2]) - 1}])"),
    (re.compile(_COL + r" > (\d+)\b"), lambda m: f"(r[{m[1]!r}] is not None and r[{m[1]!r}] > {m[2]})"),
    (re.compile(_COL + r" = '([^']*)'"), lambda m: f"(r[{m[1]!r}] == {m[2]!r})"),
    (re.compile(_COL + r" = " + _P), lambda m: f"(r[{m[1]!r}] == a[{int(m[2]) - 1}])"),
]
_HELPERS = {"_norm": _norm, "_text": _text, "_ilike": _ilike, "_lot": _lot}


def _predicate(where: str):
    """A WHERE text the services write, as a Python test of (row, args); a column's
    table alias is dropped. Also used by test_site_alias_other_modules."""
    snippets: list[str] = []

    def keep(fn):
        def sub(m):
            snippets.append(fn(m))
            return f" __T{len(snippets) - 1}__ "
        return sub

    text = where
    for pattern, fn in _TERMS:
        text = pattern.sub(keep(fn), text)
    text = re.sub(r"\bTRUE\b", " True ", text)
    for sql, py in (("AND", "and"), ("OR", "or"), ("NOT", "not")):
        text = re.sub(rf"\b{sql}\b", f" {py} ", text)
    left = re.sub(r"__T\d+__|\band\b|\bor\b|\bnot\b|\bTrue\b|[()\s]", "", text)
    assert not left, f"the fake cannot read {left!r} in: {where}"
    code = compile(re.sub(r"__T(\d+)__", lambda m: snippets[int(m[1])], text).strip(), "<where>", "eval")
    return lambda r, a: bool(eval(code, dict(_HELPERS), {"r": r, "a": a}))


class ColdDB:
    """The cold tables as lists of rows. Answers to_regclass, the grouped search
    (_SEARCH_SQL, its aggregates as Postgres computes them), the lot boxes of
    available_here (_LOT_BOXES_SQL) and a pick page (_PICK_SQL); nothing else, and
    never a write."""

    def __init__(self, **tables):
        self.tables = {name: [dict(r) for r in rows] for name, rows in tables.items()}
        self.calls: list[tuple[str, tuple]] = []

    def statements(self, needle: str) -> list[tuple[str, tuple]]:
        return [(s, a) for s, a in self.calls if needle in s]

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append((s, args))
        assert s == "SELECT to_regclass($1)", s
        return args[0] if args[0].removeprefix("public.") in self.tables else None

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append((s, args))
        assert s.startswith("SELECT ") and not re.search(r"\b(INSERT|UPDATE|DELETE)\b", s), s
        rows = self.tables[re.match(r"SELECT .+? FROM (\w+)", s)[1]]
        if " GROUP BY " in s:
            return self._groups(s, rows, args)
        if " ORDER BY id ASC LIMIT " in s:
            keep = _predicate(s.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0])
            limit = args[int(re.search(r"LIMIT \$(\d+)$", s)[1]) - 1]
            return sorted((r for r in rows if keep(r, args)), key=lambda r: r["id"])[:limit]
        if " IN (SELECT * FROM unnest(" in s:
            keep = _predicate(s.split(" WHERE ", 1)[1])
            return [{**dict(zip(LOT_COLS, _lot(r))), "box_id": r["box_id"], "transaction_no": r["transaction_no"]}
                    for r in rows if keep(r, args)]
        raise AssertionError(f"the fake has no statement: {s[:120]}")

    @staticmethod
    def _groups(s, rows, args):
        keep = _predicate(s.split(" WHERE ", 1)[1].split(" GROUP BY ", 1)[0])
        limit = args[int(re.search(r"LIMIT \$(\d+)$", s)[1]) - 1]
        groups: dict[tuple, list] = {}
        for r in rows:
            if keep(r, args):
                groups.setdefault((r["item_description"], *_lot(r)[1:]), []).append(r)
        out = []
        for key, rs in groups.items():
            def vals(c):
                return [r[c] for r in rs if r[c] is not None]
            out.append({
                "id": min(r["id"] for r in rs), "inward_dt": min(vals("inward_dt"), default=None),
                "unit": min(vals("unit"), default=None), "inward_no": key[2],
                "item_description": key[0], "item_mark": key[3], "vakkal": max(vals("vakkal"), default=None),
                "lot_no": key[1], "no_of_cartons": sum(vals("no_of_cartons")) if vals("no_of_cartons") else None,
                "weight_kg": min(vals("weight_kg"), default=None),
                "total_inventory_kgs": sum((r["no_of_cartons"] or 0) * (r["weight_kg"] or 0) for r in rs),
                "group_name": max(vals("group_name"), default=None), "storage_location": key[4],
                "exporter": max(vals("exporter"), default=None),
                "last_purchase_rate": min(vals("last_purchase_rate"), default=None),
                "value": sum(vals("value")) if vals("value") else None,
                "box_id": min(vals("box_id"), default=None),
                "transaction_no": min(vals("transaction_no"), default=None),
            })
        out.sort(key=lambda g: (g["inward_dt"] is None, g["inward_dt"] or date.min, g["id"]))
        return out[:limit]


class R3:
    """Stand-in for lookup_service.unavailable_boxes. `history` maps a box key to its
    latest transfer: (transfer id, state 'in_transit' | 'short' | 'received', To site).
    Judged as R3 judges: the transfer being edited is left out (here the box then has
    no other); received and acknowledged is free from its To site (by site), nothing
    else is. Records each call: (keys, from_warehouse, transfer_id)."""

    def __init__(self, history=None):
        self.history = history or {}
        self.calls: list[tuple[list, object, object]] = []

    async def __call__(self, conn, boxes, *, from_warehouse=None, transfer_id=None):
        boxes = list(boxes)
        self.calls.append((boxes, from_warehouse, transfer_id))
        out = []
        for key in boxes:
            last = self.history.get(key)
            if last is None or last[0] == transfer_id:
                continue
            tid, state, site = last
            if state == "received" and same_site(site, from_warehouse):
                continue
            out.append({"box_id": key[0], "transaction_no": key[1],
                        "reason": "elsewhere" if state == "received" else state,
                        "transfer_id": tid, "challan_no": f"T-{tid}", "site": site})
        return out


@pytest.fixture
def r3(monkeypatch):
    judge = R3()
    monkeypatch.setattr(lookup_service, "unavailable_boxes", judge)
    return judge


def _row(id, box_id, **over) -> dict:
    row = {"id": id, "box_id": box_id, "transaction_no": "TR-1", "item_description": "ALMOND 25KG",
           "lot_no": "LOT-9", "inward_no": "INW-881", "item_mark": "RK", "unit": "Rishi",
           "storage_location": "Rishi Cold Storage", "inward_dt": date(2026, 9, 30), "vakkal": "V-1",
           "no_of_cartons": Decimal("1"), "weight_kg": Decimal("24.75"), "total_inventory_kgs": Decimal("24.75"),
           "group_name": "NUTS", "exporter": "ACME", "last_purchase_rate": Decimal("800"), "value": Decimal("19800")}
    row.update(over)
    return row


# ── The pick ─────────────────────────────────────────────────────────────────
SAVLA = {"unit": "Savla D-39", "storage_location": "Savla D-39"}
RISHI = {"unit": "Rishi", "storage_location": "Rishi Cold Storage"}
LOT = [
    _row(90, "S-1", **SAVLA),                 # the same lot at another store, older rows
    _row(91, "S-2", **SAVLA),
    _row(101, "B-1"),                         # in transit
    _row(102, None),                          # an inner cold split row: no box identity
    _row(103, " B-3 ", transaction_no=" TR-1 "),  # padded: judged and returned trimmed
    _row(104, "B-4", transaction_no="DIRECT"),    # the quantity-only marker
    _row(105, "B-5"),                         # received at A185
    _row(106, "B-6"),                         # in transit on transfer 9
    _row(107, "B-3"),                         # B-3 listed again
    _row(108, "B-8"),
    _row(109, "B-9", lot_no="LOT-1"),         # another lot
    _row(110, "B-10", storage_location=None),  # this store's unit, no storage location
]
HISTORY = {("B-1", "TR-1"): (7, "in_transit", "A185"), ("B-5", "TR-1"): (8, "received", "A185"),
           ("B-6", "TR-1"): (9, "in_transit", "A185"), ("S-2", "TR-1"): (5, "short", "Savla D-39")}


def _pick(db, qty, **kw) -> list[str]:
    out = asyncio.run(search_service.pick_boxes(db, company="cdpl", item_description="ALMOND 25KG",
                                                lot_no="LOT-9", inward_no="INW-881", qty=qty, **kw))
    return [b["box_id"] for b in out["boxes"]]


def _lot_db() -> ColdDB:
    return ColdDB(cdpl_cold_stocks=LOT[::-1], cfpl_cold_stocks=[])


def test_without_a_from_site_the_pick_is_as_strict_as_before_and_skips_rows_it_cannot_judge(r3):
    r3.history = dict(HISTORY)
    db = _lot_db()
    before = copy.deepcopy(db.tables)
    assert _pick(db, 9) == ["S-1", "B-3", "B-8", "B-10"]
    # One judgement for the one page: the real boxes only, each once, trimmed.
    assert r3.calls == [([("S-1", "TR-1"), ("S-2", "TR-1"), ("B-1", "TR-1"), ("B-3", "TR-1"), ("B-5", "TR-1"),
                          ("B-6", "TR-1"), ("B-8", "TR-1"), ("B-10", "TR-1")], None, None)]
    assert db.tables == before and all(s.startswith("SELECT") for s, _ in db.calls)


def test_the_pick_takes_a_box_received_at_the_from_site_and_the_edited_transfers_own(r3):
    r3.history = dict(HISTORY)
    assert _pick(_lot_db(), 9, from_warehouse="A185", **RISHI) == ["B-3", "B-5", "B-8"]
    assert _pick(_lot_db(), 9, from_warehouse="A185", transfer_id=9, **RISHI) == ["B-3", "B-5", "B-6", "B-8"]
    assert _pick(_lot_db(), 9, from_warehouse="W202", transfer_id=9, **RISHI) == ["B-3", "B-6", "B-8"]
    assert {(f, t) for _, f, t in r3.calls} == {("A185", None), ("A185", 9), ("W202", 9)}


def test_the_boxes_already_on_the_list_are_skipped_and_never_judged(r3):
    out = _pick(_lot_db(), 3, exclude="B-8|TR-1, B-1 | TR-1 ,bad,|TR-1,B-5|", **RISHI)
    assert out == ["B-3", "B-5", "B-6"]
    (keys, _, _), = r3.calls
    assert ("B-8", "TR-1") not in keys and ("B-1", "TR-1") not in keys and ("B-5", "TR-1") in keys


def test_the_pick_keeps_to_the_picked_rows_store(r3):
    # Without the row's store FIFO starts at the other store's older rows (the bug);
    # with it, never.
    assert _pick(_lot_db(), 2) == ["S-1", "S-2"]
    assert _pick(_lot_db(), 2, **RISHI) == ["B-1", "B-3"]
    assert _pick(_lot_db(), 9, **SAVLA) == ["S-1", "S-2"]
    # A blank one, or one left out, matches blank or NULL (the search groups NULL with '').
    assert _pick(_lot_db(), 9, unit="Rishi") == ["B-10"]
    assert _pick(_lot_db(), 9, unit="Rishi", storage_location="") == ["B-10"]
    assert _pick(_lot_db(), 9, storage_location="Rishi Cold Storage") == []
    blank = ColdDB(cdpl_cold_stocks=[_row(1, "K-1", unit=None, storage_location=""), _row(2, "K-2")])
    assert _pick(blank, 9, unit="", storage_location="") == ["K-1"]
    assert _pick(blank, 9) == ["K-1", "K-2"]          # an old client sends neither: the whole lot
    db = _lot_db()
    _pick(db, 1, **RISHI)
    ((sql, args),) = db.statements("ORDER BY id ASC")
    assert "COALESCE(unit, '') = $4 AND COALESCE(storage_location, '') = $5" in sql
    assert args[3:5] == ("Rishi", "Rishi Cold Storage")


def test_the_pick_reads_page_after_page_until_qty_boxes_pass(r3, monkeypatch):
    monkeypatch.setattr(search_service, "_PICK_PAGE", 2)
    r3.history = dict(HISTORY)
    db = _lot_db()
    assert _pick(db, 2, from_warehouse="A185", **RISHI) == ["B-3", "B-5"]
    pages = db.statements("ORDER BY id ASC")
    assert len(pages) == 3
    assert "id >" not in pages[0][0]
    assert [(re.search(r"AND id > \$(\d+)", s)[1], a[5]) for s, a in pages[1:]] == [("6", 102), ("6", 104)]
    # Page 1's only box was refused, page 2 had one free; page 3 filled the pick.
    assert [keys for keys, _, _ in r3.calls] == [[("B-1", "TR-1")], [("B-3", "TR-1")],
                                                 [("B-5", "TR-1"), ("B-6", "TR-1")]]


def test_a_page_with_no_box_to_judge_asks_r3_nothing_and_the_lot_running_out_ends_the_pick(r3, monkeypatch):
    monkeypatch.setattr(search_service, "_PICK_PAGE", 1)
    db = ColdDB(cdpl_cold_stocks=[_row(1, None), _row(2, "B-2", transaction_no="DIRECT"), _row(3, "B-3")])
    assert _pick(db, 5) == ["B-3"]
    assert [keys for keys, _, _ in r3.calls] == [[("B-3", "TR-1")]]
    assert len(db.statements("ORDER BY id ASC")) == 4     # the fourth page is empty


def test_the_pick_404s_without_the_companys_cold_table(r3):
    with pytest.raises(HTTPException) as exc:
        _pick(ColdDB(cfpl_cold_stocks=[]), 1)
    assert exc.value.status_code == 404 and r3.calls == []


# ── The search ───────────────────────────────────────────────────────────────
CFPL = [
    _row(11, "C-1", inward_no="INW-1", unit="D-39", storage_location="OLD SAVLA", inward_dt=date(2026, 9, 2)),
    _row(12, "C-2", inward_no="INW-1", unit="D-39", storage_location="OLD SAVLA", inward_dt=date(2026, 9, 2)),
    _row(13, "C-3", inward_no="INW-1", unit="D-39", storage_location="Savla Bond", inward_dt=date(2026, 9, 4)),
    _row(14, "C-4", inward_no="INW-1", unit="D-39", storage_location="Savla", inward_dt=date(2026, 9, 5)),
    _row(15, "C-5", inward_no="INW-2", unit="Savla D-514", storage_location="Savla D-514 Cold",
         inward_dt=date(2026, 9, 6)),
    _row(16, "C-6", inward_no="INW-3", unit="Savla D-39", storage_location="Savla D-39 Chamber 3",
         inward_dt=date(2026, 9, 8)),
]
CDPL = [
    _row(11, "R-1", inward_no="INW-7", inward_dt=date(2026, 9, 1)),
    _row(12, None, inward_no="INW-7", inward_dt=date(2026, 9, 1)),
    _row(13, "R-3", inward_no="INW-8", storage_location="Cold Storage", inward_dt=date(2026, 9, 3)),
    _row(14, "P-4", inward_no="INW-9", unit="Supreme Cold", storage_location="Supreme", inward_dt=date(2026, 9, 7)),
]


def _search(db=None, **kw) -> list[dict]:
    db = db or ColdDB(cfpl_cold_stocks=CFPL, cdpl_cold_stocks=CDPL)
    return asyncio.run(search_service.search_cold_storage_stocks(db, **kw))["results"]


def _lots(results) -> list[tuple]:
    return [(r["company"], r["inward_no"], r["storage_location"]) for r in results]


def test_the_search_reads_both_companies_and_merges_their_lots_oldest_first(r3):
    assert _lots(_search(q="almond")) == [
        ("cdpl", "INW-7", "Rishi Cold Storage"), ("cfpl", "INW-1", "OLD SAVLA"), ("cdpl", "INW-8", "Cold Storage"),
        ("cfpl", "INW-1", "Savla Bond"), ("cfpl", "INW-1", "Savla"), ("cfpl", "INW-2", "Savla D-514 Cold"),
        ("cdpl", "INW-9", "Supreme"), ("cfpl", "INW-3", "Savla D-39 Chamber 3")]
    # The limit applies after the merge: cfpl alone would have given its own three.
    assert _lots(_search(lot_no="LOT-9", limit=3)) == [
        ("cdpl", "INW-7", "Rishi Cold Storage"), ("cfpl", "INW-1", "OLD SAVLA"), ("cdpl", "INW-8", "Cold Storage")]
    (one,) = _search(lot_no="LOT", limit=1)
    assert (one["id"], one["company"], one["net_qty_on_cartons"], one["available_here"]) == (11, "cdpl", 2.0, None)
    assert r3.calls == []


def test_a_company_without_its_cold_table_is_left_out():
    assert {r["company"] for r in _search(ColdDB(cdpl_cold_stocks=CDPL), q="almond")} == {"cdpl"}
    assert _search(ColdDB(), q="almond") == []


@pytest.mark.parametrize("kw, lots", [
    # A storage location is the row's store, as every reader of a cold row takes it
    # (sites.cold_row_site): its unit when that names a cold store, else its location.
    ({"storage_location": "Rishi"}, [("cdpl", "INW-7", "Rishi Cold Storage"), ("cdpl", "INW-8", "Cold Storage")]),
    ({"storage_location": "Savla D-39"}, [("cfpl", "INW-1", "OLD SAVLA"), ("cfpl", "INW-1", "Savla Bond"),
                                          ("cfpl", "INW-1", "Savla"), ("cfpl", "INW-3", "Savla D-39 Chamber 3")]),
    ({"storage_location": "savla-d39"}, [("cfpl", "INW-1", "OLD SAVLA"), ("cfpl", "INW-1", "Savla Bond"),
                                         ("cfpl", "INW-1", "Savla"), ("cfpl", "INW-3", "Savla D-39 Chamber 3")]),
    # Savla Bond and Cold Storage are sites of their own, never a store's spelling: rows of
    # unit D-39 or Rishi are at that store, whatever their location says.
    ({"storage_location": "Savla Bond"}, []),
    ({"storage_location": "Cold Storage"}, []),
    ({"unit": "Rishi"}, [("cdpl", "INW-7", "Rishi Cold Storage"), ("cdpl", "INW-8", "Cold Storage")]),
    ({"storage_location": "Savla D-514 Cold Storage"}, [("cfpl", "INW-2", "Savla D-514 Cold")]),
    # A unit compares as a site too.
    ({"unit": "Savla D-39"}, [("cfpl", "INW-1", "OLD SAVLA"), ("cfpl", "INW-1", "Savla Bond"),
                              ("cfpl", "INW-1", "Savla"), ("cfpl", "INW-3", "Savla D-39 Chamber 3")]),
    ({"unit": "Supreme"}, [("cdpl", "INW-9", "Supreme")]),
    ({"unit": "Supreme Cold Storage", "storage_location": "supreme"}, [("cdpl", "INW-9", "Supreme")]),
    ({"unit": "Rishi", "storage_location": "Savla D-39"}, []),
    ({"storage_location": "Eskimo"}, []),
])
def test_unit_and_storage_location_compare_sites(kw, lots):
    assert _lots(_search(**kw)) == lots


def test_available_here_counts_the_boxes_r3_lets_go_from_the_from_site(r3):
    r3.history = {("C-1", "TR-1"): (3, "in_transit", "A185"), ("R-3", "TR-1"): (4, "received", "A185"),
                  ("P-4", "TR-1"): (6, "short", "Rishi")}
    got = {(r["company"], r["inward_no"], r["storage_location"]): (r["net_qty_on_cartons"], r["available_here"])
           for r in _search(q="almond", from_warehouse="Rishi")}
    assert got == {
        ("cdpl", "INW-7", "Rishi Cold Storage"): (2.0, 1),   # R-1 free; the split row is no box
        ("cfpl", "INW-1", "OLD SAVLA"): (2.0, 1),            # C-1 in transit, C-2 free
        ("cdpl", "INW-8", "Cold Storage"): (1.0, 0),         # R-3 was received at A185
        ("cfpl", "INW-1", "Savla Bond"): (1.0, 1), ("cfpl", "INW-1", "Savla"): (1.0, 1),
        ("cfpl", "INW-2", "Savla D-514 Cold"): (1.0, 1), ("cfpl", "INW-3", "Savla D-39 Chamber 3"): (1.0, 1),
        ("cdpl", "INW-9", "Supreme"): (1.0, 0),              # P-4 was written off as short
    }
    # One judgement for every box of every lot found, both companies, from the site given.
    ((keys, here, tid),) = r3.calls
    assert sorted(keys) == sorted({("C-1", "TR-1"), ("C-2", "TR-1"), ("C-3", "TR-1"), ("C-4", "TR-1"),
                                   ("C-5", "TR-1"), ("C-6", "TR-1"), ("R-1", "TR-1"), ("R-3", "TR-1"),
                                   ("P-4", "TR-1")})
    assert (here, tid) == ("Rishi", None)
    from_a185 = {r["inward_no"]: r["available_here"] for r in _search(lot_no="LOT-9", from_warehouse="A185")
                 if r["company"] == "cdpl"}
    assert from_a185 == {"INW-7": 1, "INW-8": 1, "INW-9": 0}


def test_available_here_judges_only_the_lots_returned(r3):
    db = ColdDB(cfpl_cold_stocks=CFPL, cdpl_cold_stocks=CDPL)
    (only,) = _search(db, q="almond", limit=1, from_warehouse="Rishi")
    assert (only["company"], only["available_here"]) == ("cdpl", 1)
    ((keys, _, _),) = r3.calls
    assert keys == [("R-1", "TR-1")]
    # The boxes query ran on cdpl only, with the search's own filter.
    ((sql, args),) = db.statements("IN (SELECT * FROM unnest(")
    assert "FROM cdpl_cold_stocks" in sql and args[0] == "%almond%"


def test_a_blank_from_site_is_no_from_site(r3):
    assert {r["available_here"] for r in _search(q="almond", from_warehouse="  ")} == {None}
    assert r3.calls == []


# ── The routes ───────────────────────────────────────────────────────────────
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


def _client(monkeypatch, name):
    seen: dict = {}

    async def _service(conn, **kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(search_service, name, _service)
    app = FastAPI()
    app.include_router(cold_router.router)
    app.state.db_pool = _Pool(object())
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        user_id=1, phone="", full_name="Store", email="store.a185@candorfoods.in", entity="cfpl",
        role_id=2, role_name="store", is_admin=False, allowed_warehouses=["A185"])
    return TestClient(app), seen


def test_the_pick_route_passes_the_new_parameters_through(monkeypatch):
    client, seen = _client(monkeypatch, "pick_boxes")
    res = client.get("/api/v1/cold-storage/stocks/pick-boxes", params={
        "company": "cdpl", "item_description": "ALMOND 25KG", "lot_no": "LOT-9", "inward_no": "", "qty": 3,
        "from_warehouse": "A185", "transfer_id": 51260001, "exclude": "B-8|TR-1,B-1|TR-1",
        "unit": "Rishi", "storage_location": "Rishi Cold Storage", "item_mark": "RK"})
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert seen == {"company": "cdpl", "item_description": "ALMOND 25KG", "lot_no": "LOT-9", "inward_no": "",
                    "qty": 3, "from_warehouse": "A185", "transfer_id": 51260001, "exclude": "B-8|TR-1,B-1|TR-1",
                    "unit": "Rishi", "storage_location": "Rishi Cold Storage", "item_mark": "RK"}
    client.get("/api/v1/cold-storage/stocks/pick-boxes", params={
        "company": "cfpl", "item_description": "X", "lot_no": "L", "inward_no": "I", "qty": 1})
    sent = ("from_warehouse", "transfer_id", "exclude", "unit", "storage_location", "item_mark")
    assert {k: seen[k] for k in sent} == dict.fromkeys(sent)
    # A blank unit / storage location / mark (a row with none) arrives blank, not as "not sent".
    client.get("/api/v1/cold-storage/stocks/pick-boxes", params={
        "company": "cfpl", "item_description": "X", "lot_no": "L", "inward_no": "", "qty": 1,
        "unit": "", "storage_location": "", "item_mark": ""})
    assert (seen["inward_no"], seen["unit"], seen["storage_location"], seen["item_mark"]) == ("", "", "", "")


def test_the_search_route_passes_the_from_site_through(monkeypatch):
    client, seen = _client(monkeypatch, "search_cold_storage_stocks")
    assert client.get("/api/v1/cold-storage/stocks/search",
                      params={"q": "almond", "from_warehouse": "Rishi"}).status_code == 200
    assert (seen["q"], seen["from_warehouse"], seen["limit"]) == ("almond", "Rishi", 50)
    client.get("/api/v1/cold-storage/stocks/search", params={"lot_no": "LOT-9"})
    assert seen["from_warehouse"] is None
