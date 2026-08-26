"""Route + SQL-shape tests for the BOM module (/api/v1/bom/*).

Two kinds of assertion live here, and the second kind is the point of the file.

1. ROUTE BEHAVIOUR, through the real app with a stubbed pool: the aggregate
   envelope, the detail payload, the 404, and the permission gate.

2. SQL SHAPE, asserted against the statement the service actually hands to the
   connection. A FakeConn only records SQL, so nothing here can prove the query
   *runs* — but the two structural properties this feature lives or dies on are
   both textual, and both are the kind of thing a well-meaning refactor
   ("simplify that lateral into a GROUP BY") silently destroys:

     * `bom_header` must be LEFT-joined to its children. An inner join — or a
       `JOIN bom_line ... GROUP BY` — hides exactly the malformed BOMs (no
       lines, no route) that this screen exists to surface. Nothing would look
       broken; the rows would just quietly stop existing.
     * `bom_line` must NEVER be joined to `bom_process_route`. They share no FK
       and no reliable key — only free text written by two different ingest
       paths (`consumed_at_stage` vs `practical_operation`/`stage`) — so any
       join between them drops or mis-buckets lines.

   `test_aggregate_sql_uses_only_left_joins` and
   `test_aggregate_sql_never_joins_lines_to_route` fail loudly if either is
   broken. That is the only guard those invariants have without a live database.

Auth goes through the REAL dependency chain: `require_permission("bom", ...)`
calls `_extract_user` itself (it is not a `Depends(get_current_user)` the
overrides map can intercept, and this router declares no router-level
dependency), so the token round trip is faked one level lower by monkeypatching
`auth_service.validate_session`. `is_admin: True` then short-circuits the
permission check; the denial test flips it off and stubs `check_permission`.

Run:  PYTHONPATH=. python -m pytest tests/services/test_bom_router.py
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.bom.services import bom_aggregate_service

try:
    import pglast
except ImportError:  # pragma: no cover - exercised only on a box without pglast
    pglast = None

requires_pglast = pytest.mark.skipif(
    pglast is None,
    reason="pglast gives real PostgreSQL grammar validation; optional",
)

BASE = "/api/v1/bom"
AGG = f"{BASE}/aggregate"

SESSION = {
    "user_id": 11,
    "phone": "9876511111",
    "full_name": "Asha Planner",
    "email": "asha@candorfoods.in",
    "entity": "cfpl",
    "role_id": 3,
    "role_name": "planner",
    "is_admin": True,          # short-circuits require_permission, no DB call
    "role_ids": [3],
}


def _session(**over) -> dict:
    s = dict(SESSION)
    s.update(over)
    return s


# ── Fakes ────────────────────────────────────────────────────────────────────
class FakeConn:
    """Records every SQL call and replays canned rows.

    Dispatch is on the table named in the statement. The aggregate query is the
    only one that mentions `bom_header`, and the two detail queries mention
    exactly one child table each — which is itself a consequence of the
    never-join rule, so this dispatcher would break first if that rule did.
    """

    def __init__(self, *, rows=None, total=None, header=None, lines=None,
                 route=None):
        self.rows = list(rows or [])
        self.total = len(self.rows) if total is None else total
        self.header = header
        self.lines = list(lines or [])
        self.route = list(route or [])
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.total

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "bom_header" in sql:
            return self.rows
        if "bom_process_route" in sql:
            return self.route
        if "bom_line" in sql:
            return self.lines
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.header

    # -- assertion helpers --
    def sql_of(self, *needles) -> list[tuple[str, tuple]]:
        return [c for c in self.calls if all(n in c[0] for n in needles)]

    def only(self, *needles) -> tuple[str, tuple]:
        hits = self.sql_of(*needles)
        assert len(hits) == 1, f"expected one statement matching {needles}, got {len(hits)}"
        return hits[0]

    @property
    def agg_sql(self) -> str:
        """The paged aggregate SELECT (the one with LIMIT/OFFSET)."""
        return self.only("bom_header", "LIMIT")[0]

    @property
    def count_sql(self) -> str:
        return self.only("COUNT(*)\nFROM bom_header")[0]


class _FakePool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


# ── Canned rows ──────────────────────────────────────────────────────────────
# What Postgres hands back for a HEALTHY BOM: NUMERIC as Decimal, DATE as date,
# TEXT[] as list.
FULL_BOM = {
    "bom_id": 411,
    "fg_sku_name": "ROASTED CASHEW 100G",
    "customer_name": "BIGBASKET",
    "version": 2,
    "is_active": True,
    "entity": "cfpl",
    "item_group": "NUTS",
    "sub_group": "CASHEW",
    "pack_size_kg": Decimal("0.100"),
    "output_uom": "kg",
    "effective_from": date(2026, 4, 1),
    "effective_to": None,
    "line_count": 3,
    "rm_count": 1,
    "pm_count": 2,
    "other_count": 0,
    "total_qty_per_unit": Decimal("3.105"),
    "total_qty_rm": Decimal("0.105"),
    "total_qty_pm": Decimal("3.000"),
    "avg_line_loss_pct": Decimal("1.500"),
    "distinct_godowns": ["PM Store", "RM Store"],
    "has_offgrade_lines": True,
    "step_count": 4,
    "total_std_time_min": Decimal("46.00"),
    "total_route_loss_pct": Decimal("2.250"),
}

# What Postgres hands back for a BROKEN BOM — a header with no bom_line rows and
# no bom_process_route rows. It only reaches Python at all because both children
# are attached with LEFT JOIN LATERAL ... ON TRUE and the NULL aggregates are
# COALESCEd. avg_line_loss_pct stays NULL on purpose: "no lines" is not "0% loss".
ZERO_BOM = {
    "bom_id": 502,
    "fg_sku_name": "MALT BAR 40G (DRAFT)",
    "customer_name": None,
    "version": 1,
    "is_active": True,
    "entity": "cfpl",
    "item_group": "BARS",
    "sub_group": None,
    "pack_size_kg": Decimal("0.040"),
    "output_uom": "kg",
    "effective_from": date(2026, 1, 1),
    "effective_to": None,
    "line_count": 0,
    "rm_count": 0,
    "pm_count": 0,
    "other_count": 0,
    "total_qty_per_unit": Decimal("0"),
    "total_qty_rm": Decimal("0"),
    "total_qty_pm": Decimal("0"),
    "avg_line_loss_pct": None,
    "distinct_godowns": [],
    "has_offgrade_lines": False,
    "step_count": 0,
    "total_std_time_min": Decimal("0"),
    "total_route_loss_pct": Decimal("0"),
}

HEADER_ROW = {
    "bom_id": 411,
    "fg_sku_name": "ROASTED CASHEW 100G",
    "customer_name": "BIGBASKET",
    "pack_size_kg": Decimal("0.100"),
    "version": 2,
    "is_active": True,
    "effective_from": date(2026, 4, 1),
    "effective_to": None,
    "item_group": "NUTS",
    "entity": "cfpl",
    "notes": None,
    "created_at": datetime(2026, 4, 1, 6, 30, tzinfo=timezone.utc),
    "sub_group": "CASHEW",
    "bar_line_process": None,
    "output_uom": "kg",
}

LINE_ROWS = [
    {"bom_line_id": 9001, "bom_id": 411, "line_number": 1,
     "material_sku_name": "CASHEW W240", "item_type": "rm",
     "quantity_per_unit": Decimal("0.105"), "uom": "kg",
     "loss_pct": Decimal("2.000"), "godown": "RM Store",
     "can_use_offgrade": True, "offgrade_max_pct": Decimal("5.000"),
     "created_at": datetime(2026, 4, 1, 6, 30, tzinfo=timezone.utc),
     "unit_rate_inr": Decimal("780.000"), "process_stage": "roasting",
     "staging_method": "pick", "consumed_at_stage": "Final FG (opening RM)"},
    {"bom_line_id": 9002, "bom_id": 411, "line_number": 2,
     "material_sku_name": "POUCH 100G", "item_type": "pm",
     "quantity_per_unit": Decimal("1.000"), "uom": "pcs",
     "loss_pct": Decimal("1.000"), "godown": "PM Store",
     "can_use_offgrade": False, "offgrade_max_pct": Decimal("0"),
     "created_at": datetime(2026, 4, 1, 6, 30, tzinfo=timezone.utc),
     "unit_rate_inr": Decimal("1.250"), "process_stage": "packing",
     "staging_method": "backflush", "consumed_at_stage": "Final FG (packing)"},
    # item_type 'sfg' occurs in practice even though the column comment says
    # rm|pm — it must land in `other_count`, not be dropped.
    {"bom_line_id": 9003, "bom_id": 411, "line_number": 3,
     "material_sku_name": "SFG0042 ROASTED KERNEL", "item_type": "sfg",
     "quantity_per_unit": Decimal("2.000"), "uom": "kg",
     "loss_pct": Decimal("0"), "godown": None,
     "can_use_offgrade": False, "offgrade_max_pct": Decimal("0"),
     "created_at": datetime(2026, 4, 1, 6, 30, tzinfo=timezone.utc),
     "unit_rate_inr": None, "process_stage": None,
     "staging_method": "floor_stock", "consumed_at_stage": None},
]

ROUTE_ROWS = [
    {"route_id": 7001, "bom_id": 411, "step_number": 1, "process_name": "Sorting",
     "stage": "sorting", "std_time_min": Decimal("12.00"), "loss_pct": Decimal("0.500"),
     "qc_check": "visual+FM", "machine_type": "sorter",
     "created_at": datetime(2026, 4, 1, 6, 30, tzinfo=timezone.utc),
     "practical_operation": "Roast & Flavour/Salt", "stage_bucket": "Create WIP",
     "input_kind": "RM", "output_kind": "SFG", "input_code": None,
     "output_code": "SFG0042"},
    {"route_id": 7002, "bom_id": 411, "step_number": 2, "process_name": "Packing",
     "stage": "packing", "std_time_min": Decimal("34.00"), "loss_pct": Decimal("1.750"),
     "qc_check": "net weight ±2g", "machine_type": "vffs",
     "created_at": datetime(2026, 4, 1, 6, 30, tzinfo=timezone.utc),
     "practical_operation": "Pack", "stage_bucket": "Final FG",
     "input_kind": "SFG", "output_kind": "FG", "input_code": "SFG0042",
     "output_code": None},
]


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client_with(monkeypatch):
    """(conn, session) -> TestClient with the token round trip and pool stubbed.

    `require_permission` resolves the caller through `_extract_user`, which
    calls `auth_service.validate_session` (imported inside the function, so the
    module attribute is the right patch point). Faking it there exercises the
    real gate — dependency_overrides cannot reach it.
    """
    def _make(conn, session=None):
        sess = session or SESSION
        import app.modules.auth.services.auth_service as auth_service

        async def _validate(_conn, _token):
            return sess

        monkeypatch.setattr(auth_service, "validate_session", _validate)
        app.state.db_pool = _FakePool(conn)
        # No `with` block: the lifespan would try to reach the real database.
        return TestClient(app, headers={"Authorization": "Bearer test-token"})
    return _make


# ── Registration + auth floor ────────────────────────────────────────────────
@pytest.mark.parametrize("path, tag", [
    (AGG, "BOM - Aggregate"),
    (f"{BASE}/{{bom_id}}", "BOM - Core"),
])
def test_routes_are_registered_and_tagged(path, tag):
    route = next(r for r in app.routes if getattr(r, "path", None) == path)
    assert "GET" in route.methods
    # apply_module_feature_tags rewrites router tags at startup; assert on the
    # post-processed value, as test_job_work_route does.
    assert route.tags == [tag]


@pytest.mark.parametrize("path", [AGG, f"{BASE}/411"])
def test_requires_authentication(path):
    """No stubbing here — the real gate must reject an anonymous caller."""
    assert TestClient(app).get(path).status_code in (401, 403)


def test_permission_denied_for_user_without_bom_view(client_with, monkeypatch):
    """Non-admin whose roles do not carry ('bom', NULL, NULL, 'view')."""
    import app.modules.auth.services.permission_service as perm_svc

    async def _deny(*a, **k):
        return False

    monkeypatch.setattr(perm_svc, "check_permission", _deny)

    conn = FakeConn(rows=[FULL_BOM])
    res = client_with(conn, _session(is_admin=False)).get(AGG)

    assert res.status_code == 403
    assert res.json()["error"] == "forbidden"
    assert res.json()["details"]["module"] == "bom"
    assert conn.sql_of("bom_header") == [], "no query may run when the gate rejects"


def test_permission_granted_when_the_check_passes(client_with, monkeypatch):
    import app.modules.auth.services.permission_service as perm_svc

    async def _allow(*a, **k):
        return True

    monkeypatch.setattr(perm_svc, "check_permission", _allow)

    res = client_with(FakeConn(rows=[FULL_BOM]), _session(is_admin=False)).get(AGG)
    assert res.status_code == 200


# ── GET /aggregate — envelope ────────────────────────────────────────────────
def test_aggregate_returns_results_and_pagination_keys(client_with):
    conn = FakeConn(rows=[FULL_BOM], total=1)
    body = client_with(conn).get(AGG).json()

    assert set(body) == {"results", "pagination"}
    assert set(body["pagination"]) == {"page", "page_size", "total", "total_pages"}
    assert body["pagination"] == {"page": 1, "page_size": 50, "total": 1,
                                  "total_pages": 1}

    (row,) = body["results"]
    assert row["bom_id"] == 411
    # Decimal -> float, date -> ISO string, TEXT[] -> list.
    assert row["total_qty_per_unit"] == 3.105
    assert row["pack_size_kg"] == 0.1
    assert row["effective_from"] == "2026-04-01"
    assert row["distinct_godowns"] == ["PM Store", "RM Store"]
    assert row["has_offgrade_lines"] is True


def test_aggregate_row_carries_every_rolled_up_column(client_with):
    conn = FakeConn(rows=[FULL_BOM], total=1)
    (row,) = client_with(conn).get(AGG).json()["results"]

    for col in ("bom_id", "fg_sku_name", "customer_name", "version", "is_active",
                "entity", "item_group", "sub_group", "pack_size_kg", "output_uom",
                "effective_from", "effective_to", "rm_count", "pm_count",
                "other_count", "line_count", "total_qty_per_unit", "total_qty_rm",
                "total_qty_pm", "avg_line_loss_pct", "step_count",
                "total_std_time_min", "total_route_loss_pct", "distinct_godowns",
                "has_offgrade_lines"):
        assert col in row, f"aggregate row is missing {col}"


def test_total_pages_rounds_up_and_respects_page_size(client_with):
    conn = FakeConn(rows=[FULL_BOM], total=47)
    body = client_with(conn).get(AGG, params={"page": 2, "page_size": 20}).json()
    assert body["pagination"] == {"page": 2, "page_size": 20, "total": 47,
                                  "total_pages": 3}
    # OFFSET is derived from page/page_size, and both are bind params.
    assert conn.agg_sql and conn.only("bom_header", "LIMIT")[1][-2:] == (20, 20)


def test_empty_result_set_reports_zero_total_pages(client_with):
    body = client_with(FakeConn(rows=[], total=0)).get(AGG).json()
    assert body["results"] == []
    assert body["pagination"]["total"] == 0
    assert body["pagination"]["total_pages"] == 0


# ── The LEFT JOIN requirement ────────────────────────────────────────────────
def test_zero_line_zero_route_bom_still_appears_with_zeros(client_with):
    """A header with no lines AND no route steps must survive to the client.

    This is the whole reason the query uses LEFT JOIN LATERAL: these are the
    BOMs somebody opens this screen to find. See the SQL-shape tests below for
    the guard that an inner join cannot be substituted.
    """
    conn = FakeConn(rows=[ZERO_BOM, FULL_BOM], total=2)
    body = client_with(conn).get(AGG).json()

    ids = [r["bom_id"] for r in body["results"]]
    assert 502 in ids, "the zero-line/zero-route BOM was dropped"

    broken = next(r for r in body["results"] if r["bom_id"] == 502)
    assert broken["line_count"] == 0
    assert broken["rm_count"] == 0
    assert broken["pm_count"] == 0
    assert broken["other_count"] == 0
    assert broken["step_count"] == 0
    assert broken["total_qty_per_unit"] == 0
    assert broken["total_std_time_min"] == 0
    assert broken["total_route_loss_pct"] == 0
    assert broken["distinct_godowns"] == []
    assert broken["has_offgrade_lines"] is False
    # NULL, not 0 — "no lines" is not "0% loss".
    assert broken["avg_line_loss_pct"] is None


def test_aggregate_sql_uses_only_left_joins(client_with):
    """bom_header must never be inner-joined to its children.

    Fails the moment anyone writes `JOIN bom_line`, `INNER JOIN`, or a
    `JOIN ... GROUP BY` rewrite — each of which silently deletes every BOM with
    no lines (or no route) from the result set.
    """
    conn = FakeConn(rows=[FULL_BOM])
    client_with(conn).get(AGG)
    sql = conn.agg_sql

    assert "FROM bom_header h" in sql, "bom_header must be the driving table"
    assert sql.count("LEFT JOIN LATERAL") == 2, "one lateral per child table"
    assert sql.count("ON TRUE") == 2, "laterals must attach unconditionally"

    bare = re.findall(r"(?<!LEFT )JOIN\b", sql)
    assert bare == [], f"aggregate query contains a non-LEFT join: {sql}"
    assert "GROUP BY" not in sql, (
        "a GROUP BY here means the children were folded into the FROM clause, "
        "which drops childless headers"
    )


def test_aggregate_sql_never_joins_lines_to_route(client_with):
    """bom_line and bom_process_route share no key — keep the laterals disjoint.

    consumed_at_stage ('Final FG (opening RM)') and practical_operation
    ('Roast & Flavour/Salt') are written by two different ingest paths and do
    not correspond; joining on them drops or mis-buckets lines.
    """
    conn = FakeConn(rows=[FULL_BOM])
    client_with(conn).get(AGG)
    sql = conn.agg_sql

    bodies = re.findall(r"LEFT JOIN LATERAL \((.*?)\)\s+\w+\s+ON TRUE", sql, re.S)
    assert len(bodies) == 2, "could not isolate the two lateral bodies"
    for body in bodies:
        tables = {t for t in ("bom_line", "bom_process_route") if t in body}
        assert len(tables) == 1, (
            f"a lateral touches both child tables — they have no join key: {body}"
        )

    for text in ("consumed_at_stage", "practical_operation", "stage_bucket"):
        assert text not in sql, (
            f"{text} is free text from an ingest path, never a join key"
        )


def test_detail_reads_the_two_child_tables_separately(client_with):
    conn = FakeConn(header=HEADER_ROW, lines=LINE_ROWS, route=ROUTE_ROWS)
    client_with(conn).get(f"{BASE}/411")

    line_sql = conn.only("FROM bom_line")[0]
    route_sql = conn.only("FROM bom_process_route")[0]
    assert "bom_process_route" not in line_sql
    assert "bom_line" not in route_sql
    assert "JOIN" not in line_sql and "JOIN" not in route_sql
    assert "ORDER BY line_number" in line_sql
    assert "ORDER BY step_number" in route_sql


# ── Filters ──────────────────────────────────────────────────────────────────
def test_filters_reach_sql_as_bind_params_never_as_text(client_with):
    injected = "ACME'; DROP TABLE bom_header; --"
    conn = FakeConn(rows=[FULL_BOM], total=1)
    res = client_with(conn).get(AGG, params={
        "search": injected, "entity": "cfpl", "item_group": "NUTS",
        "customer_name": "BigBasket", "is_active": "true",
    })
    assert res.status_code == 200

    sql, args = conn.only("bom_header", "LIMIT")
    for hostile in (injected, "cfpl", "NUTS", "BigBasket"):
        assert hostile not in sql, "user input was interpolated into the SQL text"
    assert f"%{injected}%" in args
    # entity is an exact match (a fixed cfpl|cdpl enum, not free text), but
    # item_group and customer_name are free-text boxes and so bind as
    # case-insensitive SUBSTRING patterns -- an exact match there returns an
    # empty page for "nuts" when the column holds "NUTS", which reads to the
    # operator as "no such BOMs".
    assert "cfpl" in args
    assert "%NUTS%" in args and "%BigBasket%" in args
    assert "NUTS" not in args and "BigBasket" not in args, (
        "free-text filters must be wildcard-wrapped, not exact")
    assert True in args

    # Every filter is a $N predicate on the header (or a correlated EXISTS),
    # so the placeholders stay contiguous and end with LIMIT/OFFSET.
    assert "LIMIT $6 OFFSET $7" in sql
    assert len(args) == 7


def test_no_filters_yields_a_trivially_true_where(client_with):
    conn = FakeConn(rows=[FULL_BOM], total=1)
    client_with(conn).get(AGG)
    sql, args = conn.only("bom_header", "LIMIT")
    assert "WHERE TRUE" in sql
    assert args == (50, 0), "only LIMIT/OFFSET are bound when nothing is filtered"


def test_item_type_filter_is_an_exists_not_a_join(client_with):
    """"BOMs that HAVE at least one line of that type" — as a predicate.

    A join would both multiply the header row per matching line and turn the
    whole statement into an inner join against bom_line.
    """
    conn = FakeConn(rows=[FULL_BOM], total=1)
    client_with(conn).get(AGG, params={"item_type": "pm"})

    sql, args = conn.only("bom_header", "LIMIT")
    assert "EXISTS (SELECT 1 FROM bom_line f_bl" in sql
    assert "pm" in args
    assert re.findall(r"(?<!LEFT )JOIN\b", sql) == []
    # The laterals are untouched by the filter — childless BOMs of the right
    # type are still reachable.
    assert sql.count("LEFT JOIN LATERAL") == 2


def test_count_applies_exactly_the_same_filters_as_the_page(client_with):
    """A total that ignores a filter is a lie in the pagination footer."""
    conn = FakeConn(rows=[FULL_BOM], total=1)
    client_with(conn).get(AGG, params={
        "entity": "cdpl", "is_active": "false", "item_type": "rm",
    })

    count_sql, count_args = conn.only("COUNT(*)\nFROM bom_header")
    page_sql, page_args = conn.only("bom_header", "LIMIT")

    where = count_sql.split("WHERE", 1)[1].strip()
    assert where in page_sql, "count and page query disagree on the WHERE clause"
    # The page query carries the same params plus LIMIT/OFFSET on the end.
    assert page_args[:len(count_args)] == count_args
    assert page_args[len(count_args):] == (50, 0)
    assert count_args == ("cdpl", False, "rm")


def test_is_active_false_is_not_swallowed_as_falsy(client_with):
    """`is_active=false` must filter; only an ABSENT value means "no filter"."""
    conn = FakeConn(rows=[], total=0)
    client_with(conn).get(AGG, params={"is_active": "false"})
    sql, args = conn.only("bom_header", "LIMIT")
    assert "h.is_active = $1" in sql
    assert args[0] is False


# ── GET /{bom_id} ────────────────────────────────────────────────────────────
def test_detail_returns_header_lines_route_and_counts(client_with):
    conn = FakeConn(header=HEADER_ROW, lines=LINE_ROWS, route=ROUTE_ROWS)
    body = client_with(conn).get(f"{BASE}/411").json()

    assert set(body) == {"header", "lines", "route", "counts"}
    assert body["header"]["bom_id"] == 411
    assert body["header"]["pack_size_kg"] == 0.1
    assert body["header"]["created_at"] == "2026-04-01T06:30:00+00:00"

    assert [l["line_number"] for l in body["lines"]] == [1, 2, 3]
    assert body["lines"][0]["quantity_per_unit"] == 0.105
    # consumed_at_stage / process_stage are plain COLUMNS, never a nesting key.
    assert body["lines"][0]["consumed_at_stage"] == "Final FG (opening RM)"
    assert body["lines"][0]["process_stage"] == "roasting"

    assert [s["step_number"] for s in body["route"]] == [1, 2]
    assert body["route"][0]["practical_operation"] == "Roast & Flavour/Salt"
    # The route is a flat ordered strip — no line ever hangs off a step.
    assert all("lines" not in s for s in body["route"])

    # LINE_ROWS is one rm + one pm + one 'sfg'; the sfg line falls into `other`.
    assert body["counts"] == {"line_count": 3, "rm_count": 1, "pm_count": 1,
                              "other_count": 1, "step_count": 2}


def test_detail_counts_bucket_sfg_lines_as_other(client_with):
    """'sfg' occurs in bom_line.item_type in practice; it must be counted."""
    conn = FakeConn(header=HEADER_ROW, lines=LINE_ROWS, route=ROUTE_ROWS)
    counts = client_with(conn).get(f"{BASE}/411").json()["counts"]
    # LINE_ROWS is rm + pm + sfg.
    assert counts["rm_count"] + counts["pm_count"] + counts["other_count"] \
        == counts["line_count"]


def test_detail_of_a_bom_with_no_children_returns_empty_collections(client_with):
    conn = FakeConn(header=HEADER_ROW, lines=[], route=[])
    body = client_with(conn).get(f"{BASE}/411").json()
    assert body["lines"] == []
    assert body["route"] == []
    assert body["counts"] == {"line_count": 0, "rm_count": 0, "pm_count": 0,
                              "other_count": 0, "step_count": 0}


def test_detail_404s_for_an_unknown_bom_id(client_with):
    conn = FakeConn(header=None)
    res = client_with(conn).get(f"{BASE}/999999")

    assert res.status_code == 404
    assert res.json()["error"] == "bom_not_found"
    assert res.json()["details"]["bom_id"] == 999999
    # No child lookups once the header is missing.
    assert conn.sql_of("FROM bom_line") == []
    assert conn.sql_of("FROM bom_process_route") == []


# ── Service-level unit checks (no HTTP) ──────────────────────────────────────
@pytest.mark.asyncio
async def test_service_serialises_decimals_dates_and_arrays():
    conn = FakeConn(rows=[ZERO_BOM], total=1)
    out = await bom_aggregate_service.list_bom_aggregate(conn)
    (row,) = out["results"]
    assert isinstance(row["pack_size_kg"], float)
    assert row["effective_from"] == "2026-01-01"
    assert row["effective_to"] is None
    assert row["distinct_godowns"] == []


@pytest.mark.asyncio
async def test_service_returns_none_for_a_missing_bom():
    assert await bom_aggregate_service.get_bom_detail(FakeConn(header=None), 1) is None


# ── Real PostgreSQL grammar validation ───────────────────────────────────────
# The FakeConn above only RECORDS SQL, so it happily passes on a statement that
# would not survive PREPARE (the exact failure test_box_lookup_live_sql.py was
# written after). pglast is libpg_query — the actual PostgreSQL parser — so this
# catches a malformed FILTER / LATERAL / ARRAY_AGG before it reaches RDS.
_S = bom_aggregate_service

_FILTER_COMBOS = [
    {},
    {"entity": "cfpl"},
    {"search": "cashew"},
    {"item_type": "pm"},
    {"is_active": False},
    {"search": "bar", "entity": "cdpl", "item_group": "BARS",
     "customer_name": "BigBasket", "is_active": True, "item_type": "rm"},
]


@requires_pglast
@pytest.mark.parametrize("filters", _FILTER_COMBOS)
def test_generated_sql_is_valid_postgres(filters):
    kwargs = {"search": None, "entity": None, "item_group": None,
              "customer_name": None, "is_active": None, "item_type": None}
    kwargs.update(filters)
    where, params, idx = _S._build_filters(**kwargs)

    pglast.parse_sql(_S._COUNT_SQL.format(where=where))
    pglast.parse_sql(_S._AGGREGATE_SQL.format(
        where=where, limit_idx=idx, offset_idx=idx + 1))

    # Placeholders are contiguous $1..$N and every one has a bound value.
    used = {int(n) for n in re.findall(r"\$(\d+)", where)}
    assert used == set(range(1, len(params) + 1)) or not params
    assert idx == len(params) + 1


@requires_pglast
@pytest.mark.parametrize("sql", [_S._HEADER_SQL, _S._LINES_SQL, _S._ROUTE_SQL])
def test_detail_sql_is_valid_postgres(sql):
    pglast.parse_sql(sql)


@requires_pglast
def test_rbac_migration_is_valid_postgres():
    """095_bom_module_rbac.sql must parse — migrate.py executes it whole."""
    from pathlib import Path

    path = (Path(__file__).parents[2] / "app" / "db" / "095_bom_module_rbac.sql")
    sql = path.read_text(encoding="utf-8")
    stmts = pglast.parse_sql(sql)
    assert len(stmts) == 2, "one catalog seed + one admin grant"
    # The catalog row is the two-NULL tuple the router gates on, and its
    # idempotency comes from the NOT EXISTS guard, not from ON CONFLICT:
    # auth_permission's UNIQUE is NULLS DISTINCT.
    assert "'bom'" in sql and "NULL::text" in sql
    assert "NOT EXISTS" in sql and "IS NOT DISTINCT FROM" in sql
    assert "ON CONFLICT" in sql


@requires_pglast
def test_rbac_migration_grants_bom_view_to_admin_only():
    """The BOM module is admin-only for now, and that is enforced in the DATA:
    the catalog row exists so the permission can be granted later, but only
    'admin' is granted it. If a role grant is ever added here, the UI must be
    updated in step -- lib/modules.tsx MODULES "BOM" carries adminOnly, and a
    SCOPED role additionally needs a ROLE_MODULE_SCOPE entry, because the
    scoped branch of the /modules tile filter never consults adminOnly.
    """
    from pathlib import Path

    path = (Path(__file__).parents[2] / "app" / "db" / "095_bom_module_rbac.sql")
    sql = path.read_text(encoding="utf-8")

    granted = set(re.findall(r"r\.role_name\s*(?:=|IN)\s*\(?\s*'([^']+)'", sql))
    # IN (...) lists carry extra names after the first; sweep them all up.
    for m in re.finditer(r"r\.role_name\s+IN\s*\(([^)]*)\)", sql):
        granted.update(re.findall(r"'([^']+)'", m.group(1)))

    assert granted == {"admin"}, (
        f"095 grants bom.view to {sorted(granted)}; the module is meant to be "
        f"admin-only, so the tile in lib/modules.tsx and any ROLE_MODULE_SCOPE "
        f"entry must be updated together with this file")


def test_migration_is_registered_in_the_runner():
    from pathlib import Path

    runner = (Path(__file__).parents[2] / "scripts" / "migrate.py").read_text(
        encoding="utf-8")
    assert '"095_bom_module_rbac.sql"' in runner


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/v1/bom — create
# ═══════════════════════════════════════════════════════════════════════════
class WriteConn:
    """Records writes for the create path.

    `existing` is the active-BOM conflict probe (fetchrow); `prior_max` is the
    version lineage probe (fetchval). Both default to "nothing there", i.e. the
    happy path, so a test only states the bit it is about.
    """

    def __init__(self, *, existing=None, prior_max=None, new_bom_id=900):
        self.existing = existing
        self.prior_max = prior_max
        self.new_bom_id = new_bom_id
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.existing

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "MAX(version)" in sql:
            return self.prior_max
        if "INSERT INTO bom_header" in sql:
            return self.new_bom_id
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql, *args):
        # Only reached for a NON-admin caller: check_permission short-circuits
        # on is_admin, so an admin session never queries the catalog. Returning
        # no rows is precisely "this role holds no matching permission", which
        # is what the denial test asserts on.
        self.calls.append((sql, args))
        return []

    def transaction(self):
        # Recorded, because otherwise nothing in the suite can tell whether the
        # writes are transactional: deleting `async with conn.transaction()`
        # from the router left every create test green.
        calls = self.calls

        class _Tx:
            async def __aenter__(_s):
                calls.append(("BEGIN", ()))
                return None

            async def __aexit__(_s, *exc):
                calls.append(("COMMIT" if exc[0] is None else "ROLLBACK", ()))
                return False
        return _Tx()

    def sql_of(self, *needles):
        return [c for c in self.calls if all(n in c[0] for n in needles)]


def _line(**over) -> dict:
    ln = {"material_sku_name": "CASHEW W240", "item_type": "rm",
          "quantity_per_unit": 0.5}
    ln.update(over)
    return ln


def _payload(**over) -> dict:
    p = {"fg_sku_name": "ROASTED CASHEW 100G", "entity": "cfpl",
         "lines": [_line()]}
    p.update(over)
    return p


def test_create_writes_header_lines_and_route(client_with):
    conn = WriteConn(new_bom_id=901)
    res = client_with(conn).post(BASE, json=_payload(
        customer_name="BIGBASKET", pack_size_kg=0.1, item_group="NUTS",
        floors=["Floor 2"], machines=["Roaster-1"],
        lines=[_line(), _line(material_sku_name="POUCH 100G", item_type="pm",
                     quantity_per_unit=1, uom="nos", godown="PM Store")],
        route=[{"process_name": "Sorting", "std_time_min": 20},
               {"process_name": "Metal Detection"}],
    ))

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["bom_id"] == 901
    assert body["version"] == 1          # no prior lineage
    assert body["lines_created"] == 2
    assert body["route_steps_created"] == 2
    # created_by comes from the token, never the body.
    assert body["created_by"] == SESSION["full_name"]

    assert len(conn.sql_of("INSERT INTO bom_header")) == 1
    assert len(conn.sql_of("INSERT INTO bom_line")) == 2
    assert len(conn.sql_of("INSERT INTO bom_process_route")) == 2


def test_create_assigns_line_and_step_numbers_from_array_order(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        lines=[_line(material_sku_name="A"), _line(material_sku_name="B"),
               _line(material_sku_name="C")],
        route=[{"process_name": "One"}, {"process_name": "Two"}],
    ))
    assert res.status_code == 201, res.text

    # line_number is $2 and is never taken from the client.
    assert [a[1] for _, a in conn.sql_of("INSERT INTO bom_line")] == [1, 2, 3]
    assert [a[1] for _, a in conn.sql_of("INSERT INTO bom_process_route")] == [1, 2]


def test_create_rejects_when_an_active_bom_already_exists(client_with):
    """Strict create: 409, and nothing is written or deactivated."""
    conn = WriteConn(existing={"bom_id": 411, "version": 2, "entity": "cfpl"})
    res = client_with(conn).post(BASE, json=_payload())

    assert res.status_code == 409, res.text
    detail = res.json()
    assert detail["error"] == "bom_exists"
    assert detail["details"]["bom_id"] == 411

    assert conn.sql_of("INSERT INTO") == []
    # The incumbent must NOT be deactivated -- that is what separates this from
    # plan_v2.create_bom, and a regression would silently orphan live job cards.
    assert conn.sql_of("is_active = FALSE") == []
    assert conn.sql_of("UPDATE bom_header") == []


def test_create_version_continues_the_lineage_past_deactivated_headers(client_with):
    """MAX(version) is NOT filtered by is_active: reusing a version number would
    stop (fg_sku_name, version) identifying one recipe."""
    conn = WriteConn(prior_max=4)
    res = client_with(conn).post(BASE, json=_payload())
    assert res.status_code == 201, res.text
    assert res.json()["version"] == 5

    sql, _ = conn.sql_of("MAX(version)")[0]
    assert "is_active" not in sql


def test_create_accepts_sfg_lines(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        lines=[_line(item_type="sfg", material_sku_name="SFG0042",
                     consumed_at_stage="Final FG (opening RM)")]))
    assert res.status_code == 201, res.text
    assert res.json()["lines_created"] == 1


@pytest.mark.parametrize("bad, code", [
    ({"item_type": "raw"}, "bad_item_type"),
    ({"quantity_per_unit": 0}, "bad_qty"),
    ({"quantity_per_unit": -1}, "bad_qty"),
    ({"material_sku_name": "   "}, "no_material"),
    ({"staging_method": "teleport"}, "bad_staging_method"),
])
def test_create_rejects_bad_lines_with_400_and_writes_nothing(client_with, bad, code):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(lines=[_line(**bad)]))
    assert res.status_code == 400, res.text
    assert res.json()["error"] == code
    assert conn.sql_of("INSERT INTO") == []


def test_create_rejects_a_bad_entity(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(entity="acme"))
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "bad_entity"
    assert conn.sql_of("INSERT INTO") == []


def test_create_requires_at_least_one_line(client_with):
    """Enforced by Pydantic (min_length=1) before the service is reached."""
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(lines=[]))
    assert res.status_code == 422, res.text
    assert conn.sql_of("INSERT INTO") == []


def test_norm_name_folds_nbsp_which_a_naive_space_collapse_would_not():
    """Unit-level, with a negative control.

    The previous version of this test could not fail: Python's str.split()
    already treats U+00A0 as whitespace, so ' '.join(v.split()) folds it with or
    without explicit NBSP handling. The control below is the implementation that
    would actually be wrong -- a plain-space collapse -- and it must disagree.
    """
    from app.modules.bom.services.bom_write_service import _norm_name
    import re

    naive = lambda v: re.sub(r" +", " ", v).strip()   # noqa: E731 - the control

    assert _norm_name("A B") == "A B"
    assert naive("A B") != "A B", "control must NOT fold NBSP"
    assert _norm_name("ROASTED CASHEW  100G") == "ROASTED CASHEW 100G"
    assert _norm_name("  padded  name  ") == "padded name"
    assert _norm_name(None) == ""


def test_create_probe_normalises_the_stored_COLUMN_not_just_the_input(client_with):
    """The bug this replaces: normalising only the bind parameter.

    An incumbent stored as 'ROASTED CASHEW 100G' is invisible to
    `fg_sku_name ILIKE 'ROASTED CASHEW 100G'`, so no 409 fires and
    uq_bom_header_active_fg does not fire either (the literal strings differ) --
    two ACTIVE headers for one SKU. Both probes must therefore fold the COLUMN.
    """
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        fg_sku_name="ROASTED CASHEW  100G",
        lines=[_line(material_sku_name="CASHEW  W240")]))
    assert res.status_code == 201, res.text
    assert res.json()["fg_sku_name"] == "ROASTED CASHEW 100G"

    probes = conn.sql_of("bom_header", "chr(160)")
    assert len(probes) == 2, (
        "both the is_active conflict probe AND the MAX(version) lineage probe "
        f"must fold the column, got {len(probes)}: {[p[0] for p in probes]}")
    for sql, args in probes:
        assert "ILIKE" not in sql, (
            "ILIKE treats the bind value as a PATTERN: an FG name containing "
            "_ or % would match unrelated SKUs")
        assert "regexp_replace" in sql and "lower(" in sql
        assert args[0] == "ROASTED CASHEW 100G"

    _, line_args = conn.sql_of("INSERT INTO bom_line")[0]
    assert "CASHEW W240" in line_args


def test_create_warns_that_tally_may_delete_rm_pm_lines(client_with):
    """Unconditional when rm/pm lines exist.

    Gating this on "the FG has prior BOM history" was backwards: the refresh
    selects candidates `WHERE is_active` and matches by name, never by version
    history, so a first-ever BOM for a Tally-known FG -- the highest-risk case --
    got no warning at all.
    """
    conn = WriteConn(prior_max=3)
    res = client_with(conn).post(BASE, json=_payload(
        lines=[_line(), _line(material_sku_name="SFG1", item_type="sfg")]))
    assert res.status_code == 201, res.text
    warnings = res.json()["warnings"]
    assert any("Tally" in w and "DELETE" in w for w in warnings), warnings
    # Counts only the at-risk lines: sfg is excluded because the refresh keeps it.
    assert any("1 of 2 lines are rm/pm" in w for w in warnings), warnings


def test_create_warns_about_tally_even_for_a_brand_new_fg(client_with):
    """The regression guard for the inverted predicate.

    A first-ever BOM (prior_max None) for an FG that Tally already exports is
    is_active=TRUE, so it is a refresh candidate on the very next run. This is
    the case the old `had_prior_header` gate silently skipped.
    """
    conn = WriteConn(prior_max=None)
    res = client_with(conn).post(BASE, json=_payload())
    assert res.status_code == 201, res.text
    assert any("Tally" in w and "DELETE" in w for w in res.json()["warnings"])


def test_create_does_not_warn_about_tally_for_an_all_sfg_bom(client_with):
    """sfg is the one line type the refresh never deletes, so nothing is at risk."""
    conn = WriteConn(prior_max=3)
    res = client_with(conn).post(BASE, json=_payload(
        lines=[_line(item_type="sfg", material_sku_name="SFG0001"),
               _line(item_type="sfg", material_sku_name="SFG0002")]))
    assert res.status_code == 201, res.text
    assert not any("Tally" in w for w in res.json()["warnings"])


def test_create_warns_but_allows_a_material_on_two_lines(client_with):
    """consumed_at_stage exists so one material CAN appear at two stages, but the
    Tally refresh culls the duplicates, so say so."""
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(lines=[
        _line(consumed_at_stage="Roast"), _line(consumed_at_stage="Pack")]))
    assert res.status_code == 201, res.text
    assert res.json()["lines_created"] == 2
    assert any("more than one line" in w for w in res.json()["warnings"])


def test_create_defaults_route_stage_to_a_slug_of_the_process_name(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        route=[{"process_name": "Metal Detection"}]))
    assert res.status_code == 201, res.text
    _, args = conn.sql_of("INSERT INTO bom_process_route")[0]
    assert "metal_detection" in args


def test_create_omits_tolerance_when_not_supplied(client_with):
    """allowed_balance_tolerance_pct is NOT NULL DEFAULT 0.001, so naming it with
    a None would violate the column."""
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload())
    assert res.status_code == 201, res.text
    sql, _ = conn.sql_of("INSERT INTO bom_header")[0]
    assert "allowed_balance_tolerance_pct" not in sql

    conn2 = WriteConn()
    res2 = client_with(conn2).post(BASE, json=_payload(
        allowed_balance_tolerance_pct=0.005))
    assert res2.status_code == 201, res2.text
    sql2, args2 = conn2.sql_of("INSERT INTO bom_header")[0]
    assert "allowed_balance_tolerance_pct" in sql2 and 0.005 in args2


def test_create_never_writes_client_supplied_bom_id_or_version(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        bom_id=1, version=99, is_active=False))
    assert res.status_code == 201, res.text
    assert res.json()["bom_id"] == 900 and res.json()["version"] == 1
    sql, args = conn.sql_of("INSERT INTO bom_header")[0]
    # Positional, not `in`: `True in args` matched the version element via
    # True == 1, so it passed even with is_active=False appended.
    cols = _header_cols_of(sql)
    row = dict(zip(cols, args))
    assert row["is_active"] is True
    assert row["version"] == 1
    assert "bom_id" not in cols


def test_create_is_denied_without_the_bom_create_permission(client_with):
    conn = WriteConn()
    res = client_with(conn, _session(is_admin=False, role_name="viewer")).post(
        BASE, json=_payload())
    assert res.status_code == 403, res.text
    assert conn.sql_of("INSERT INTO") == []


# ── Helpers + the coverage the review found missing ──────────────────────────
def _header_cols_of(sql: str) -> list[str]:
    """Column list out of `INSERT INTO bom_header (a, b, c) VALUES ...`."""
    inner = sql.split("(", 1)[1].split(")", 1)[0]
    return [c.strip() for c in inner.split(",")]


def _cols_of(sql: str) -> list[str]:
    return _header_cols_of(sql)


def test_create_runs_every_write_inside_one_transaction(client_with):
    """Deleting `async with conn.transaction()` used to leave the suite green.

    Without it a failure on the third of five statements commits a half BOM that
    is is_active=TRUE, so plan_v2.create_plan resolves it and indents against a
    partial recipe.
    """
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        lines=[_line(), _line(material_sku_name="B")],
        route=[{"process_name": "One"}]))
    assert res.status_code == 201, res.text

    kinds = [sql for sql, _ in conn.calls]
    assert kinds.count("BEGIN") == 1, kinds
    begin_at = kinds.index("BEGIN")
    inserts = [i for i, k in enumerate(kinds) if k.startswith("INSERT INTO")]
    assert inserts, "no INSERT recorded"
    assert min(inserts) > begin_at, "a write happened before BEGIN"
    assert kinds[-1] == "COMMIT"


def test_create_round_trips_every_whitelisted_header_column(client_with):
    """Dropping a column from _HEADER_COLS was invisible to the whole suite.

    A silently-NULL pack_size_kg makes every kg<->unit conversion on that BOM
    wrong, and nothing would have failed.
    """
    from app.modules.bom.services.bom_write_service import _HEADER_COLS

    sent = {
        "fg_sku_name": "AGG TEST FG", "customer_name": "ACME", "entity": "cfpl",
        "pack_size_kg": 0.25, "output_uom": "kg", "item_group": "NUTS",
        "sub_group": "CASHEW", "process_category": "Roast", "business_unit": "Retail",
        "factory": "Bhiwandi", "floors": ["Floor 2"], "machines": ["Roaster-1"],
        "bar_line_process": "Roast + Pack", "shelf_life_days": 180,
        "gst_rate": 5.0, "hsn_sac": "20081910", "inventory_group": "FG",
        "customer_code": "ACME-1", "effective_from": "2026-08-01",
        "effective_to": "2027-08-01", "notes": "hello",
    }
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=dict(sent, lines=[_line()]))
    assert res.status_code == 201, res.text

    sql, args = conn.sql_of("INSERT INTO bom_header")[0]
    row = dict(zip(_cols_of(sql), args))
    for col in _HEADER_COLS:
        assert col in row, f"{col} is in _HEADER_COLS but never reached the INSERT"

    assert row["pack_size_kg"] == 0.25
    assert row["floors"] == ["Floor 2"] and row["machines"] == ["Roaster-1"]
    assert row["shelf_life_days"] == 180
    assert row["notes"] == "hello"
    assert str(row["effective_from"]) == "2026-08-01"
    assert str(row["effective_to"]) == "2027-08-01"


def test_create_round_trips_every_whitelisted_line_column(client_with):
    from app.modules.bom.services.bom_write_service import _LINE_COLS

    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(lines=[_line(
        uom="kg", loss_pct=2.5, godown="RM Store", can_use_offgrade=True,
        offgrade_max_pct=10, unit_rate_inr=720.5, process_stage="Roast",
        staging_method="backflush", consumed_at_stage="Final FG (opening RM)")]))
    assert res.status_code == 201, res.text

    sql, args = conn.sql_of("INSERT INTO bom_line")[0]
    # bom_id, line_number, then _LINE_COLS in order.
    row = dict(zip(("bom_id", "line_number") + tuple(_LINE_COLS), args))
    for col in _LINE_COLS:
        assert col in row, f"{col} is in _LINE_COLS but never reached the INSERT"
    assert row["consumed_at_stage"] == "Final FG (opening RM)"
    assert row["staging_method"] == "backflush"
    assert row["can_use_offgrade"] is True
    assert row["godown"] == "RM Store"


def test_create_round_trips_every_whitelisted_route_column(client_with):
    from app.modules.bom.services.bom_write_service import _ROUTE_COLS

    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(route=[{
        "process_name": "Sorting", "stage": "sorting", "std_time_min": 20,
        "loss_pct": 0.5, "qc_check": "visual+FM", "machine_type": "Table",
        "practical_operation": "Sort", "stage_bucket": "Create WIP",
        "input_kind": "RM", "output_kind": "WIP",
        "input_code": "SFG1", "output_code": "SFG2"}]))
    assert res.status_code == 201, res.text

    sql, args = conn.sql_of("INSERT INTO bom_process_route")[0]
    row = dict(zip(("bom_id", "step_number") + tuple(_ROUTE_COLS), args))
    for col in _ROUTE_COLS:
        assert col in row, f"{col} is in _ROUTE_COLS but never reached the INSERT"
    assert row["qc_check"] == "visual+FM" and row["output_code"] == "SFG2"


def test_every_generated_write_binds_exactly_as_many_args_as_placeholders(client_with):
    """The placeholder lists are hand-computed with range(3, 3 + len(...)).

    A future edit that prepends a fixed column without shifting the range start
    binds one arg too many; asyncpg raises at runtime and a FakeConn suite stays
    green, because nothing compares max($n) against len(args).
    """
    import re

    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        allowed_balance_tolerance_pct=0.002,
        lines=[_line(), _line(material_sku_name="B")],
        route=[{"process_name": "One"}, {"process_name": "Two"}]))
    assert res.status_code == 201, res.text

    writes = [(sql, args) for sql, args in conn.calls if sql.startswith("INSERT INTO")]
    assert len(writes) == 5, [w[0][:40] for w in writes]
    for sql, args in writes:
        nums = {int(n) for n in re.findall(r"\$(\d+)", sql)}
        assert nums == set(range(1, len(args) + 1)), (
            f"placeholders {sorted(nums)} vs {len(args)} bound args in {sql[:80]}")


def test_create_omits_route_inserts_when_no_route_given(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload())
    assert res.status_code == 201, res.text
    assert res.json()["route_steps_created"] == 0
    assert conn.sql_of("INSERT INTO bom_process_route") == []


def test_create_stores_a_whitespace_only_customer_name_as_null(client_with):
    """NULL means "generic BOM"; an empty string is neither generic nor
    customer-specific and matches no lookup on either side."""
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(customer_name="   "))
    assert res.status_code == 201, res.text
    sql, args = conn.sql_of("INSERT INTO bom_header")[0]
    assert dict(zip(_cols_of(sql), args))["customer_name"] is None


def test_create_duplicate_material_warning_is_case_insensitive(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(lines=[
        _line(material_sku_name="CASHEW W240"),
        _line(material_sku_name="cashew w240")]))
    assert res.status_code == 201, res.text
    assert any("more than one line" in w for w in res.json()["warnings"])


def test_create_defaults_effective_from_when_omitted(client_with):
    """bom_header.effective_from has NO database default, and the column is
    always named, so an omitted value would bind an explicit NULL."""
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload())
    assert res.status_code == 201, res.text
    sql, args = conn.sql_of("INSERT INTO bom_header")[0]
    assert dict(zip(_cols_of(sql), args))["effective_from"] is not None


@pytest.mark.parametrize("qty", [0.0004, 0.0001])
def test_create_rejects_a_quantity_that_rounds_to_zero(client_with, qty):
    """NUMERIC(15,3): 0.0004 stores as 0.000. The line then sits on the BOM
    looking present while the planner indents nothing for it."""
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        lines=[_line(quantity_per_unit=qty)]))
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "qty_below_precision"
    assert conn.sql_of("INSERT INTO") == []


def test_create_accepts_the_smallest_representable_quantity(client_with):
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        lines=[_line(quantity_per_unit=0.001)]))
    assert res.status_code == 201, res.text


@pytest.mark.parametrize("tol", [2, 10, 1.5, -0.1])
def test_create_rejects_a_tolerance_outside_the_fraction_range(client_with, tol):
    """It is compared as abs(diff)/total_input <= tolerance, so 2 ("2%") accepts
    a 200% imbalance and switches the R9 close gate off; >= 10 overflows
    NUMERIC(5,4) into an unhandled 500."""
    conn = WriteConn()
    res = client_with(conn).post(BASE, json=_payload(
        allowed_balance_tolerance_pct=tol))
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "bad_tolerance"
    assert conn.sql_of("INSERT INTO") == []


def test_create_returns_409_when_the_unique_index_catches_a_race(client_with):
    """The probe is a check-then-insert with no lock. uq_bom_header_active_fg is
    what actually prevents the duplicate; without this the loser of a race got a
    500 where a sequential double-submit gets a 409."""
    import asyncpg

    class RacingConn(WriteConn):
        async def fetchval(self, sql, *args):
            self.calls.append((sql, args))
            if "MAX(version)" in sql:
                return None
            if "INSERT INTO bom_header" in sql:
                raise asyncpg.UniqueViolationError(
                    "duplicate key value violates unique constraint "
                    '"uq_bom_header_active_fg"')
            return None

    conn = RacingConn()
    res = client_with(conn).post(BASE, json=_payload())
    assert res.status_code == 409, res.text
    assert res.json()["error"] == "bom_exists"
    assert conn.sql_of("INSERT INTO bom_line") == []


def test_rbac_migration_seeds_the_create_tuple_too():
    """Deleting the create VALUES row from 095 left every other assertion green:
    statement count is unchanged and the admin grant has no action predicate.
    Ship it missing and POST /api/v1/bom 403s for every non-admin forever."""
    from pathlib import Path

    sql = (Path(__file__).parents[2] / "app" / "db"
           / "095_bom_module_rbac.sql").read_text(encoding="utf-8")
    body = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
    for action in ("'view'", "'create'"):
        assert action in body, f"095 must seed the {action} tuple"


def test_create_is_denied_for_a_role_holding_only_bom_view(client_with):
    """The old denial test returned [] for EVERY permission query, so it proved
    nothing about the action string -- switching the gate to action="view" kept
    it green. This one grants view and expects create to still be refused."""
    class ViewerConn(WriteConn):
        async def fetch(self, sql, *args):
            self.calls.append((sql, args))
            # check_permission binds (role_ids, module, sub, subsub, action) and
            # selects the three scope columns. Granting ONLY 'view' is what makes
            # this test meaningful: a gate on action="view" would be allowed
            # here, so the 403 proves the endpoint really asks for 'create'.
            if len(args) >= 5 and args[1] == "bom" and args[4] == "view":
                return [{"allowed_entities": None,
                         "allowed_warehouses": None,
                         "allowed_floors": None}]
            return []

    conn = ViewerConn()
    res = client_with(conn, _session(is_admin=False, role_name="viewer")).post(
        BASE, json=_payload())
    assert res.status_code == 403, res.text
    assert conn.sql_of("INSERT INTO") == []
