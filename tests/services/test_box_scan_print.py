"""Manual print on the job card's Raw Material tab — Stores' Manual print, recorded
straight onto the job card.

Each printed box is minted in sfg_box as an RM box (stage_bucket 'Raw Material')
and scanned into jc_box_scan for this job card, in the same transaction: no
floor_requisition_box row, no request. The job card's list of scans says which
boxes were printed here (their sticker "Box #" and LOT) and the next free Box #.
The SQL runs for real only on a DB; here a scripted connection answers it.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pydantic
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.routing import Match

from app.modules.floor_requisition.services import box_service as stores
from app.modules.floor_requisition.services.requisition_service import RequisitionError
from app.modules.production import router as PR
from app.modules.production.services import box_scan_service as bss
from app.modules.production.services import sfg_box_service

JC = 75009889
SEEDS = "Sunflower Seeds Roasted"
JC_ROW = {"job_card_number": "PLAN-7-L1-S2", "entity": "cfpl", "floor": "Mezzanine"}

# A cast right after "$n" is the type PostgreSQL gives that parameter, and asyncpg
# refuses an argument of another Python type (an int bound to $1::text fails with
# "expected str, got int"). A fake would take it, so every call is checked here.
_CAST = re.compile(r"\$(\d+)::(\w+(?:\[\])?)")
_BINDS = {"text": str, "bigint": int, "int": int, "integer": int, "text[]": list}


def _check_binds(sql, args):
    for n, cast in _CAST.findall(sql):
        value = args[int(n) - 1]
        if value is None or cast not in _BINDS:
            continue
        assert isinstance(value, _BINDS[cast]) and not isinstance(value, bool), f"${n}::{cast} bound to {value!r}"
        if cast == "text[]":
            assert all(isinstance(v, str) for v in value), f"${n}::{cast} bound to {value!r}"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    """Answers each statement by its opening words and logs every call in order."""

    def __init__(self, *, jc=JC_ROW, last=0, scans=(), printed=(), clashes=0):
        self.jc, self.last = jc, last
        self.scans, self.printed = list(scans), list(printed)
        self.clashes = clashes                       # sfg_box PK clashes before an insert lands
        self.calls: list[tuple[str, str, tuple]] = []

    def is_in_transaction(self):
        return True

    def transaction(self):
        return _Tx()

    def _log(self, kind, sql, args):
        s = " ".join(sql.split())
        _check_binds(s, args)
        self.calls.append((kind, s, args))
        return s

    async def fetchrow(self, sql, *args):
        s = self._log("fetchrow", sql, args)
        if s.startswith("SELECT job_card_number, entity, floor FROM job_card_v2"):
            return self.jc
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = self._log("fetchval", sql, args)
        if s.startswith("SELECT COALESCE(MAX(CAST(split_part(carton_id"):
            return self.last
        if s.startswith("INSERT INTO sfg_box"):
            if self.clashes:
                self.clashes -= 1
                clash = asyncpg.UniqueViolationError('duplicate key value violates unique constraint "sfg_box_pkey"')
                clash.constraint_name = "sfg_box_pkey"
                raise clash
            return args[0]
        if s.startswith("SELECT to_regclass('floor_requisition_box')"):
            return False
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def execute(self, sql, *args):
        s = self._log("execute", sql, args)
        if s.startswith("SELECT pg_advisory_xact_lock"):
            return "SELECT 1"
        if s.startswith("INSERT INTO jc_box_scan"):
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {s[:90]}")

    async def fetch(self, sql, *args):
        s = self._log("fetch", sql, args)
        if s.startswith("SELECT * FROM jc_box_scan_enriched"):
            return self.scans
        if s.startswith("SELECT carton_id, batch_code FROM sfg_box"):
            return self.printed
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    def ran(self, prefix):
        return [c for c in self.calls if c[1].startswith(prefix)]

    def first(self, prefix):
        return next(i for i, c in enumerate(self.calls) if c[1].startswith(prefix))


def _written(call) -> dict:
    """The row an `INSERT INTO t (cols) VALUES (...)` call writes, column by column."""
    _, sql, args = call
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    vals = [v.strip() for v in sql.split("VALUES (", 1)[1].split(")", 1)[0].split(",")]

    def value(token):
        if token.startswith("$"):
            return args[int(token[1:]) - 1]
        return None if token == "NULL" else token.strip("'")

    assert len(cols) == len(vals)
    return dict(zip(cols, map(value, vals)))


def run(coro):
    return asyncio.run(coro)


def _line(n, net=9.77, gross=10.0, count=3, lot="L-7"):
    return {"box_number": n, "net_weight": net, "gross_weight": gross, "count": count, "lot_number": lot}


def printed(conn, boxes, article=SEEDS, actor="Floor Fay"):
    return run(bss.print_boxes(conn, job_card_id=JC, article=article, boxes=boxes, actor=actor))


def _refused(conn, boxes, article=SEEDS):
    with pytest.raises(RequisitionError) as e:
        printed(conn, boxes, article=article)
    return e.value


# ── print ──
def test_print_mints_rm_boxes_and_scans_each_into_the_job_card(monkeypatch):
    monkeypatch.setattr(bss, "new_short_time_id", lambda: 48213307)
    conn = FakeConn(last=4)
    out = printed(conn, [_line(5), _line(6, net=5, gross=None, count=None, lot="  ")],
                  article="  Sunflower Seeds Roasted ")

    sfg = [_written(c) for c in conn.ran("INSERT INTO sfg_box")]
    assert sfg[0] == {"carton_id": "48213307-5", "item_type": "rm", "job_card_id": JC,
                      "job_card_number": "PLAN-7-L1-S2", "sfg_code": SEEDS, "fg_sku_name": SEEDS,
                      "entity": "cfpl", "floor": "Mezzanine", "stage_bucket": "Raw Material",
                      "batch_code": "L-7", "net_weight": 9.77, "gross_weight": 10.0, "units": 3,
                      "status": "PRINTED", "created_by": "Floor Fay"}
    assert (sfg[1]["carton_id"], sfg[1]["batch_code"], sfg[1]["net_weight"], sfg[1]["gross_weight"],
            sfg[1]["units"]) == ("48213307-6", None, 5.0, None, None)

    scans = [_written(c) for c in conn.ran("INSERT INTO jc_box_scan")]
    assert scans == [
        {"job_card_id": JC, "batch_id": None, "sfg_box_id": "48213307-5", "article": SEEDS,
         "net_weight": 9.77, "gross_weight": 10.0, "count": 3, "scanned_by": "Floor Fay"},
        {"job_card_id": JC, "batch_id": None, "sfg_box_id": "48213307-6", "article": SEEDS,
         "net_weight": 5.0, "gross_weight": None, "count": None, "scanned_by": "Floor Fay"},
    ]
    assert out == {"job_card_id": JC, "job_card_number": "PLAN-7-L1-S2", "entity": "cfpl", "boxes": [
        {"box_code": "48213307-5", "box_number": 5, "article": SEEDS, "net_weight": 9.77,
         "gross_weight": 10.0, "count": 3, "lot_number": "L-7"},
        {"box_code": "48213307-6", "box_number": 6, "article": SEEDS, "net_weight": 5.0,
         "gross_weight": None, "count": None, "lot_number": None},
    ]}


def test_each_box_goes_into_sfg_box_before_its_scan_in_the_order_sent(monkeypatch):
    monkeypatch.setattr(bss, "new_short_time_id", lambda: 11111111)
    conn = FakeConn(last=0)
    out = printed(conn, [_line(7), _line(2)])
    writes = [(c[1].split(" (")[0], c[2]) for c in conn.calls if c[1].startswith("INSERT")]
    assert [w[0] for w in writes] == ["INSERT INTO sfg_box", "INSERT INTO jc_box_scan"] * 2
    assert [_written(c)["sfg_box_id"] for c in conn.ran("INSERT INTO jc_box_scan")] == ["11111111-7", "11111111-2"]
    assert [b["box_number"] for b in out["boxes"]] == [7, 2]


def test_nothing_ties_the_box_to_a_stores_request(monkeypatch):
    monkeypatch.setattr(bss, "new_short_time_id", lambda: 11111111)
    conn = FakeConn()
    printed(conn, [_line(1)])
    assert not any("floor_requisition" in c[1] for c in conn.calls)


def test_the_lock_is_taken_before_the_counter_is_read():
    conn = FakeConn(last=2)
    printed(conn, [_line(3)])
    lock, counter = conn.first("SELECT pg_advisory_xact_lock"), conn.first("SELECT COALESCE(MAX(")
    assert lock < counter < conn.first("INSERT INTO sfg_box")
    kind, sql, args = conn.calls[lock]
    # The job card id is a number, so its parameter is bigint (asyncpg would refuse
    # an int for a text one) and only the key is text.
    assert "pg_advisory_xact_lock(hashtextextended('jc_box_print:' || $1::bigint::text, 0))" in sql
    assert args == (JC,) and type(args[0]) is int
    # The same per-job-card counter Stores' Manual print and the WIP boxes use.
    assert conn.calls[counter][1] == " ".join(stores._COUNTER_SQL.split()) and conn.calls[counter][2] == (JC,)


def test_a_text_cast_parameter_bound_to_a_number_is_caught():
    # The guard every FakeConn call goes through: this pairing fails on a real DB.
    with pytest.raises(AssertionError, match="bound to"):
        _check_binds("SELECT pg_advisory_xact_lock(hashtextextended('jc_box_print:' || $1::text, 0))", (JC,))
    _check_binds("SELECT pg_advisory_xact_lock(hashtextextended('jc_box_print:' || $1::bigint::text, 0))", (JC,))


class _WipLockOnly:
    """Just enough connection for create_wip_boxes to take its lock and find no job card."""

    def __init__(self):
        self.locks = []

    async def execute(self, sql, *args):
        self.locks.append((" ".join(sql.split()), args))
        return "SELECT 1"

    async def fetchrow(self, sql, *args):
        return None


def test_a_print_waits_for_wip_boxes_being_made_on_the_same_job_card():
    # Both read the job card's one counter, so both must hold the lock create_wip_boxes
    # takes: otherwise each could read the same last number and mint the same Box #.
    wip = _WipLockOnly()
    assert run(sfg_box_service.create_wip_boxes(wip, JC, [{"net_weight": 1}]))["error"] == "not_found"
    [wip_lock] = wip.locks
    conn = FakeConn(last=2)
    printed(conn, [_line(3)])
    held = [i for i, c in enumerate(conn.calls) if (c[1], c[2]) == wip_lock]
    assert held and held[0] < conn.first("SELECT COALESCE(MAX(")


def test_the_job_card_is_read_live_only():
    conn = FakeConn()
    printed(conn, [_line(1)])
    kind, sql, args = conn.ran("SELECT job_card_number, entity, floor FROM job_card_v2")[0]
    assert "WHERE job_card_id = $1 AND deleted_at IS NULL" in sql and args == (JC,)


def test_a_box_number_already_used_on_the_job_card_is_refused_before_anything_is_written():
    conn = FakeConn(last=4)
    err = _refused(conn, [_line(3), _line(5), _line(4)])
    assert (err.status, err.error) == (409, "box_number_taken")
    assert err.message == "Box number 3, 4 is already used on this job card. Generate the boxes again."
    assert err.details == {"box_numbers": [3, 4], "next_box_number": 5}
    assert not conn.ran("INSERT")


def test_a_missing_or_deleted_job_card_is_404():
    conn = FakeConn(jc=None)
    err = _refused(conn, [_line(1)])
    assert (err.status, err.error, err.message) == (404, "job_card_not_found", "Job card not found.")
    assert not conn.ran("SELECT pg_advisory_xact_lock") and not conn.ran("INSERT")


@pytest.mark.parametrize("article, boxes, error, message", [
    ("   ", [_line(1)], "article_required", "Choose the article to print."),
    ("x" * 501, [_line(1)], "article_too_long", "The article name is over 500 characters."),
    (SEEDS, [], "bad_box_count", "Print between 1 and 500 boxes at a time."),
    (SEEDS, [_line(n) for n in range(1, 502)], "bad_box_count", "Print between 1 and 500 boxes at a time."),
    (SEEDS, [_line(1), _line(1)], "duplicate_box_number", "The same box number appears twice."),
    (SEEDS, [_line(0)], "bad_box_number", "Every box needs a box number of 1 or more."),
    (SEEDS, [_line("3")], "bad_box_number", "Every box needs a box number of 1 or more."),
    (SEEDS, [_line(1), _line(7, net=0)], "bad_box", "Box 7: net wt must be more than 0."),
    (SEEDS, [_line(1, net=float("inf"))], "bad_box", "Box 1: net wt must be more than 0."),
    (SEEDS, [_line(1, net=5, gross=4.99)], "bad_box", "Box 1: gross wt can't be less than net wt."),
    (SEEDS, [_line(1, count=-1)], "bad_box", "Box 1: count must be a whole number, 0 or more."),
    (SEEDS, [_line(1, count=1.5)], "bad_box", "Box 1: count must be a whole number, 0 or more."),
    (SEEDS, [_line(1), _line(2 ** 31)], "bad_box_number",
     "Box number 2147483648 is too high. Box numbers go up to 99999999."),
    (SEEDS, [_line(100_000_000)], "bad_box_number", "Box number 100000000 is too high. Box numbers go up to 99999999."),
])
def test_a_bad_print_is_refused_in_plain_english_and_nothing_is_written(article, boxes, error, message):
    conn = FakeConn()
    err = _refused(conn, boxes, article=article)
    assert (err.status, err.error, err.message) == (400, error, message)
    assert not conn.ran("SELECT pg_advisory_xact_lock") and not conn.ran("INSERT")


def test_the_highest_box_number_is_printed(monkeypatch):
    # The number is the carton id's counter, which the job card's counter reads as an
    # integer (int4): the ceiling keeps it, and the WIP boxes' last + n, well inside.
    monkeypatch.setattr(bss, "new_short_time_id", lambda: 11111111)
    out = printed(FakeConn(), [_line(99_999_999)])
    assert out["boxes"][0]["box_code"] == "11111111-99999999"


def test_the_article_limit_counts_the_trimmed_name():
    conn = FakeConn()
    out = printed(conn, [_line(1)], article="  " + "x" * 500 + "  ")
    assert out["boxes"][0]["article"] == "x" * 500


def test_a_pk_clash_rolls_a_new_time(monkeypatch):
    times = iter([48213307, 48213309])
    monkeypatch.setattr(bss, "new_short_time_id", lambda: next(times))
    conn = FakeConn(clashes=1)
    out = printed(conn, [_line(1)])
    assert [c[2][0] for c in conn.ran("INSERT INTO sfg_box")] == ["48213307-1", "48213309-1"]
    assert out["boxes"][0]["box_code"] == "48213309-1"
    assert _written(conn.ran("INSERT INTO jc_box_scan")[0])["sfg_box_id"] == "48213309-1"


# ── the list ──
def _scan_row(**over):
    row = {"job_card_id": JC, "box_id": None, "sfg_box_id": None, "transaction_no": None, "article": SEEDS,
           "net_weight": Decimal("9.770"), "gross_weight": Decimal("10.000"), "count": 3}
    row.update(over)
    return row


def listed(conn):
    return run(bss.list_scans(conn, job_card_id=JC))


@pytest.mark.parametrize("last, want", [(0, 1), (None, 1), (9, 10)])
def test_the_list_gives_the_next_box_number(last, want):
    conn = FakeConn(last=last)
    out = listed(conn)
    assert out["next_box_number"] == want
    kind, sql, args = conn.ran("SELECT COALESCE(MAX(")[0]
    assert sql == " ".join(stores._COUNTER_SQL.split()) and args == (JC,)


def test_the_list_marks_the_boxes_printed_on_this_tab():
    scans = [_scan_row(sfg_box_id="48213307-5"), _scan_row(sfg_box_id="9568129-1"),
             _scan_row(box_id="BX-1", transaction_no="TR-1")]
    conn = FakeConn(scans=scans, printed=[{"carton_id": "48213307-5", "batch_code": "L-7"}])
    out = listed(conn)
    assert [s["printed"] for s in out["scans"]] == [{"box_number": 5, "lot_number": "L-7"}, None, None]


def test_the_list_asks_once_for_this_job_cards_rm_boxes_printed_here():
    scans = [_scan_row(sfg_box_id="48213307-5"), _scan_row(box_id="BX-1"), _scan_row(sfg_box_id="9568129-1")]
    conn = FakeConn(scans=scans)
    listed(conn)
    [(kind, sql, args)] = conn.ran("SELECT carton_id, batch_code FROM sfg_box")
    assert "carton_id = ANY($1::text[])" in sql and "job_card_id = $2" in sql
    assert "item_type = 'rm'" in sql and "stage_bucket = $3" in sql
    assert args == (["48213307-5", "9568129-1"], JC, "Raw Material")


@pytest.mark.parametrize("carton_id, number", [
    ("48213307-12", 12), ("48213307-007", 7), ("LEGACY-A", None), ("12345678", None), ("48213307-", None),
    ("48213307-²", None),
])
def test_the_printed_box_number_is_the_ids_counter(carton_id, number):
    conn = FakeConn(scans=[_scan_row(sfg_box_id=carton_id)],
                    printed=[{"carton_id": carton_id, "batch_code": None}])
    [scan] = listed(conn)["scans"]
    assert scan["printed"] == {"box_number": number, "lot_number": None}


def test_no_printed_lookup_without_sfg_boxes():
    conn = FakeConn(scans=[_scan_row(box_id="BX-1"), _scan_row(box_id="BX-2")])
    out = listed(conn)
    assert not conn.ran("SELECT carton_id, batch_code") and [s["printed"] for s in out["scans"]] == [None, None]
    empty = FakeConn()
    assert listed(empty)["scans"] == [] and not empty.ran("SELECT carton_id, batch_code")


def test_printed_refs_leave_stores_refs_and_the_totals_alone():
    scans = [_scan_row(sfg_box_id="48213307-5"), _scan_row(box_id="BX-1", net_weight=Decimal("1.250"),
                                                           gross_weight=None, count=None)]
    out = listed(FakeConn(scans=scans, printed=[{"carton_id": "48213307-5", "batch_code": None}]))
    assert set(out) == {"job_card_id", "scans", "totals", "next_box_number"}
    assert [s["stores"] for s in out["scans"]] == [None, None]
    assert out["totals"] == {"boxes": 2, "net_weight": 11.02, "gross_weight": 10.0, "count": 3}


# ── the route ──
PRINT_PATH = "/api/v1/production/job-cards-v2/{job_card_id}/box-scans/print"


class _Ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class _TxConn:
    def __init__(self):
        self.opened = 0

    def transaction(self):
        self.opened += 1
        return _Ctx(self)


def _request():
    conn = _TxConn()
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool))), conn


def _body(**over):
    body = {"article": SEEDS, "boxes": [{"box_number": 2, "net_weight": 9.5, "lot_number": "L1"}]}
    body.update(over)
    return PR.BoxPrintRequest(**body)


def test_the_print_route_runs_the_service_in_one_transaction(monkeypatch):
    seen = {}

    async def print_boxes(conn, **kw):
        seen.update(kw)
        return {"job_card_id": kw["job_card_id"], "boxes": []}

    monkeypatch.setattr(bss, "print_boxes", print_boxes)
    req, conn = _request()
    user = SimpleNamespace(full_name="", phone="9800000000")
    out = run(PR.print_box_scans(req, JC, _body(), user=user))
    assert out == {"job_card_id": JC, "boxes": []} and conn.opened == 1
    assert seen == {"job_card_id": JC, "article": SEEDS, "actor": "9800000000",
                    "boxes": [{"box_number": 2, "net_weight": 9.5, "gross_weight": None, "count": None,
                               "lot_number": "L1"}]}


def test_the_print_route_turns_a_refusal_into_error_message_and_details(monkeypatch):
    async def refuse(conn, **kw):
        raise RequisitionError(409, "box_number_taken",
                               "Box number 3 is already used on this job card. Generate the boxes again.",
                               box_numbers=[3], next_box_number=5)

    monkeypatch.setattr(bss, "print_boxes", refuse)
    req, _ = _request()
    with pytest.raises(HTTPException) as e:
        run(PR.print_box_scans(req, JC, _body(), user=SimpleNamespace(full_name="Floor Fay", phone=None)))
    assert e.value.status_code == 409
    assert e.value.detail == {"error": "box_number_taken",
                              "message": "Box number 3 is already used on this job card. Generate the boxes again.",
                              "box_numbers": [3], "next_box_number": 5}


def test_the_service_refuses_what_the_body_lets_through():
    # The body bounds the payload only; the plain-English refusals are the service's.
    body = _body(article="   ", boxes=[{"box_number": 0, "net_weight": 0, "count": -1}])
    assert body.article == "   " and body.boxes[0].box_number == 0
    with pytest.raises(pydantic.ValidationError):
        _body(article="x" * 2001)
    with pytest.raises(pydantic.ValidationError):
        _body(boxes=[{"box_number": 1, "net_weight": 1, "lot_number": "L" * 101}])


def test_the_body_cannot_name_who_printed():
    for model in (PR.BoxPrintRequest, PR.BoxPrintLine):
        assert not {"actor", "created_by", "scanned_by", "recorded_by"} & set(model.model_fields)


def _route(path, method):
    return next(r for r in PR.router.routes
                if isinstance(r, APIRoute) and r.path == path and method in r.methods)


def test_printing_needs_the_same_permission_as_scanning():
    def permission(route):
        for dep in route.dependant.dependencies:
            code, cells = getattr(dep.call, "__code__", None), getattr(dep.call, "__closure__", None)
            if code and cells:
                names = dict(zip(code.co_freevars, (c.cell_contents for c in cells)))
                if "module" in names and "action" in names:
                    return names["module"], names["sub_module"], names["sub_sub_module"], names["action"]
        return None

    scan = _route("/api/v1/production/job-cards-v2/{job_card_id}/box-scans", "POST")
    assert permission(_route(PRINT_PATH, "POST")) == permission(scan) == \
        ("production", "job_cards", "material_scan", "scan")


@pytest.mark.parametrize("method, path, endpoint", [
    ("POST", "/job-cards-v2/75009889/box-scans/print", "print_box_scans"),
    ("POST", "/job-cards-v2/75009889/box-scans", "create_box_scan"),
    ("GET", "/job-cards-v2/75009889/box-scans", "list_box_scans"),
    ("DELETE", "/job-cards-v2/75009889/box-scans/48213307-5", "delete_box_scan"),
    ("DELETE", "/job-cards-v2/75009889/box-scans/print", "delete_box_scan"),
])
def test_each_box_scans_path_reaches_its_own_route(method, path, endpoint):
    scope = {"type": "http", "method": method, "path": "/api/v1/production" + path, "root_path": ""}
    for route in PR.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            assert route.endpoint.__name__ == endpoint
            return
    raise AssertionError(f"no route takes {method} {path}")
