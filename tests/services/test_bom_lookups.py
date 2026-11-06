"""GET /api/v1/bom/lookups/materials and /lookups/processes.

The type-ahead sources behind the BOM form's Material and Process pickers.
Same FakeConn style as the other BOM suites: what matters is the SHAPE of the
SQL (user text only ever reaches Postgres as a bind parameter, LIKE
metacharacters are escaped, a blank query never dumps the master) and the
response envelope the form reads.

Run:  PYTHONPATH=. python -m pytest tests/services/test_bom_lookups.py
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.bom.services import bom_lookup_service as svc
from tests.services.test_bom_router import (  # noqa: F401 - client_with is a fixture
    BASE, client_with,
)

try:
    import pglast
except ImportError:  # pragma: no cover
    pglast = None

requires_pglast = pytest.mark.skipif(pglast is None, reason="pglast optional")

MATERIALS = f"{BASE}/lookups/materials"
PROCESSES = f"{BASE}/lookups/processes"

SKU_ROWS = [
    {"sku_id": 1, "particulars": "Carnival Roasted & Salted Cashew 230g",
     "item_type": "fg", "item_group": "Nuts", "sub_group": "Cashew"},
    {"sku_id": 2, "particulars": "CASHEW W240", "item_type": "rm",
     "item_group": "Nuts", "sub_group": None},
]
PROCESS_ROWS = [
    {"process_name": "Sorting + Packing", "stage": "packing", "boms": 643},
    {"process_name": "Create WIP: Roasting", "stage": "roasting", "boms": 41},
]


class LookupConn:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM all_sku" in sql or "FROM bom_process_route" in sql:
            return self.rows
        return []   # permission catalog

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return None

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return None

    def sql_of(self, *needles):
        return [c for c in self.calls if all(n in c[0] for n in needles)]


# ── Registration + auth ──────────────────────────────────────────────────────
@pytest.mark.parametrize("path", [MATERIALS, PROCESSES])
def test_lookup_routes_are_registered(path):
    hits = [r for r in app.routes if getattr(r, "path", None) == path and "GET" in r.methods]
    assert len(hits) == 1
    assert hits[0].tags == ["BOM - Lookups"]


@pytest.mark.parametrize("path", [MATERIALS, PROCESSES])
def test_lookup_routes_require_authentication(path):
    assert TestClient(app).get(path, params={"q": "x"}).status_code in (401, 403)


@pytest.mark.parametrize("path", [MATERIALS, PROCESSES])
def test_lookups_need_only_a_login_not_bom_view(client_with, monkeypatch, path):
    """Shared reference data for pickers embedded in other modules' screens
    (the plan-builder on SO Creation is used by planners, who never hold the
    admin-only bom.view). A non-admin with NO bom permission at all must still
    get answers; the catalog is never even consulted."""
    import app.modules.auth.services.permission_service as perm_svc
    from tests.services.test_bom_router import _session

    async def _deny(*a, **k):
        raise AssertionError("lookups must not consult the permission catalog")

    monkeypatch.setattr(perm_svc, "check_permission", _deny)
    conn = LookupConn(SKU_ROWS if path == MATERIALS else PROCESS_ROWS)
    res = client_with(conn, _session(is_admin=False, role_name="planner")).get(path, params={"q": "c"})
    assert res.status_code == 200, res.text
    assert len(res.json()["results"]) == 2


# ── Materials ────────────────────────────────────────────────────────────────
def test_materials_returns_ranked_rows_with_type(client_with):
    conn = LookupConn(SKU_ROWS)
    res = client_with(conn).get(MATERIALS, params={"q": "cashew"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"results"}
    assert [r["sku_id"] for r in body["results"]] == [1, 2]
    assert body["results"][0]["item_type"] == "fg"      # FG kept: hamper components
    assert set(body["results"][0]) == {"sku_id", "particulars", "item_type",
                                       "item_group", "sub_group"}


def test_materials_blank_query_returns_nothing_without_touching_the_db(client_with):
    conn = LookupConn(SKU_ROWS)
    for q in ("", "   "):
        res = client_with(conn).get(MATERIALS, params={"q": q})
        assert res.status_code == 200
        assert res.json() == {"results": []}
    assert conn.sql_of("all_sku") == []


def test_materials_tokens_bind_as_a_pattern_never_as_sql_text(client_with):
    conn = LookupConn(SKU_ROWS)
    hostile = "roasted  cashew'; DROP TABLE all_sku; --"
    res = client_with(conn).get(MATERIALS, params={"q": hostile})
    assert res.status_code == 200, res.text

    ((sql, args),) = conn.sql_of("FROM all_sku")
    assert "DROP" not in sql and "cashew" not in sql
    # Every token must appear, in order, case-folded; the prefix form ranks
    # names that START with the query first; LIMIT is bound, not inlined.
    # The whole thing, DROP included, travels as a lower-cased LIKE pattern:
    # one wildcard between tokens, LIKE metacharacters escaped.
    assert args[0].startswith(r"%roasted%cashew';%drop%table%all\_sku;%--%")
    assert args[1].startswith(r"roasted cashew'; drop table all\_sku; --")
    assert args[-1] == 20
    assert "LIMIT $3" in sql
    assert "ORDER BY (lower(particulars) LIKE $2) DESC" in sql


def test_materials_escapes_like_metacharacters():
    """'PM24_Hamper' must not match 'PM24-Hamper' through the _ wildcard, and a
    literal % must not swallow the rest of the name."""
    assert svc._token_pattern("PM24_Hamper 100%") == "%pm24\\_hamper%100\\%%"
    assert svc._escape_like("a\\b") == "a\\\\b"


def test_materials_item_type_filter_is_bound_and_lowercased(client_with):
    conn = LookupConn(SKU_ROWS)
    res = client_with(conn).get(MATERIALS, params={"q": "cashew", "item_type": "RM", "limit": 5})
    assert res.status_code == 200, res.text
    ((sql, args),) = conn.sql_of("FROM all_sku")
    assert "AND lower(item_type) = $3" in sql
    assert args[2] == "rm" and args[3] == 5
    assert "LIMIT $4" in sql


def test_materials_omits_the_type_clause_when_no_type_given(client_with):
    conn = LookupConn(SKU_ROWS)
    client_with(conn).get(MATERIALS, params={"q": "cashew"})
    ((sql, _),) = conn.sql_of("FROM all_sku")
    assert "item_type) =" not in sql
    # Unlike /so/sku-lookup, sfg is NOT excluded by default.
    assert "sfg" not in sql


@pytest.mark.parametrize("limit", [0, 101])
def test_materials_limit_is_bounded(client_with, limit):
    res = client_with(LookupConn()).get(MATERIALS, params={"q": "x", "limit": limit})
    assert res.status_code == 422


# ── Processes ────────────────────────────────────────────────────────────────
def test_processes_lists_everything_for_a_blank_query(client_with):
    conn = LookupConn(PROCESS_ROWS)
    res = client_with(conn).get(PROCESSES)
    assert res.status_code == 200, res.text
    assert [r["process_name"] for r in res.json()["results"]] == [
        "Sorting + Packing", "Create WIP: Roasting"]
    ((sql, args),) = conn.sql_of("FROM bom_process_route")
    assert "LIKE" not in sql, "blank query must not add a filter"
    assert "GROUP BY process_name" in sql
    assert "COUNT(DISTINCT bom_id) AS boms" in sql
    assert "ORDER BY boms DESC" in sql
    assert args == (30,)


def test_processes_substring_binds_as_a_pattern(client_with):
    conn = LookupConn(PROCESS_ROWS)
    res = client_with(conn).get(PROCESSES, params={"q": "Create WIP", "limit": 10})
    assert res.status_code == 200, res.text
    ((sql, args),) = conn.sql_of("FROM bom_process_route")
    assert "AND lower(process_name) LIKE $1" in sql
    assert args == ("%create%wip%", 10)
    assert "Create" not in sql


def test_process_rows_carry_stage_and_usage(client_with):
    res = client_with(LookupConn(PROCESS_ROWS)).get(PROCESSES)
    row = res.json()["results"][0]
    assert row == {"process_name": "Sorting + Packing", "stage": "packing", "boms": 643}


# ── Grammar ──────────────────────────────────────────────────────────────────
@requires_pglast
@pytest.mark.parametrize("sql", [
    svc._MATERIAL_SQL.format(type_clause="", limit_idx=3),
    svc._MATERIAL_SQL.format(type_clause="AND lower(item_type) = $3", limit_idx=4),
    svc._PROCESS_SQL.format(q_clause="", limit_idx=1),
    svc._PROCESS_SQL.format(q_clause="AND lower(process_name) LIKE $1", limit_idx=2),
])
def test_lookup_sql_is_valid_postgres(sql):
    pglast.parse_sql(sql)
    used = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert used == set(range(1, max(used) + 1)), "placeholders must be contiguous"
