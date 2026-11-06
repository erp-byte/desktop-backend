"""PATCH /api/v1/bom/{bom_id} and DELETE /api/v1/bom/{bom_id}.

Same approach as test_bom_router.py (whose fixtures and canned rows this file
imports): the real app, the real permission gate with the token round trip
faked one level down, and a connection fake that only RECORDS SQL. So, as there,
the load-bearing assertions are about the SHAPE of what reaches the database:

  * DELETE never deletes. The only statement that touches bom_header is an
    UPDATE of is_active, and nothing touches bom_line / bom_process_route.
  * PATCH touches only the columns that were SENT, never version / bom_id /
    created_at, and runs every write inside the one outer transaction so a
    refusal mid-way (a referenced line) rolls the header change back too.
  * Line replacement is matched by bom_line_id: an id that is sent is UPDATEd
    (the id survives, so job-card rows keep pointing at it), an unknown id is a
    400, a missing id is a DELETE, and a DELETE the FK refuses becomes one 409
    listing every such line.

Run:  PYTHONPATH=. python -m pytest tests/services/test_bom_router_update_delete.py
"""
from __future__ import annotations

import re
from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.bom.services import bom_write_service as svc
# Fixtures imported into this module's namespace are collected by pytest.
from tests.services.test_bom_router import (  # noqa: F401 - client_with is a fixture
    BASE, HEADER_ROW, LINE_ROWS, ROUTE_ROWS, SESSION, _line, _session, client_with,
)

try:
    import pglast
except ImportError:  # pragma: no cover
    pglast = None

requires_pglast = pytest.mark.skipif(pglast is None, reason="pglast optional")

BOM = f"{BASE}/411"
EXISTING_LINE_IDS = tuple(r["bom_line_id"] for r in LINE_ROWS)   # 9001, 9002, 9003


# ── Fake connection for the write paths ──────────────────────────────────────
class UpdateConn:
    """Records every statement; answers the handful of reads the services make.

    `header`          the FOR UPDATE row (None -> 404)
    `line_ids`        what `SELECT bom_line_id FROM bom_line` returns
    `clash`           the other-active-BOM probe's row (None -> no conflict)
    `fk_blocked`      bom_line_ids whose DELETE raises ForeignKeyViolationError
    `unique_on_update` make the header UPDATE raise UniqueViolationError
    """

    def __init__(self, *, header=HEADER_ROW, line_ids=EXISTING_LINE_IDS,
                 clash=None, fk_blocked=(), unique_on_update=False):
        self.header = header
        self.line_ids = list(line_ids)
        self.clash = clash
        self.fk_blocked = set(fk_blocked)
        self.unique_on_update = unique_on_update
        self.calls: list[tuple[str, tuple]] = []
        self.depth = 0

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "bom_id <> $2" in sql:
            return self.clash
        if "FROM bom_header" in sql:
            return self.header
        return None

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "SELECT bom_line_id FROM bom_line" in sql:
            return [{"bom_line_id": i} for i in self.line_ids]
        if "FROM bom_process_route" in sql:
            return ROUTE_ROWS
        if "FROM bom_line" in sql:
            return LINE_ROWS
        return []   # permission catalog for a non-admin caller

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith("DELETE FROM bom_line ") and args and args[0] in self.fk_blocked:
            raise asyncpg.ForeignKeyViolationError(
                'update or delete on table "bom_line" violates foreign key '
                'constraint on table "job_card_rm_indent_v2"')
        if sql.startswith("UPDATE bom_header SET") and self.unique_on_update:
            raise asyncpg.UniqueViolationError(
                'duplicate key value violates unique constraint '
                '"uq_bom_header_active_fg"')
        return "UPDATE 1"

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(_s):
                conn.calls.append(("BEGIN" if conn.depth == 0 else "SAVEPOINT", ()))
                conn.depth += 1
                return None

            async def __aexit__(_s, *exc):
                conn.depth -= 1
                if exc[0] is None:
                    conn.calls.append(("COMMIT" if conn.depth == 0 else "RELEASE", ()))
                else:
                    conn.calls.append(("ROLLBACK" if conn.depth == 0 else "ROLLBACK TO", ()))
                return False
        return _Tx()

    # -- assertion helpers --
    def sql_of(self, *needles):
        return [c for c in self.calls if all(n in c[0] for n in needles)]

    def kinds(self):
        return [sql for sql, _ in self.calls]

    def writes(self):
        return [(s, a) for s, a in self.calls
                if s.startswith(("INSERT INTO", "UPDATE ", "DELETE FROM"))]


def _ulines(*items):
    """Lines payload: `int` -> keep that existing id (with a stub body),
    `dict` -> as given."""
    out = []
    for it in items:
        if isinstance(it, int):
            out.append(_line(bom_line_id=it, material_sku_name=f"KEPT {it}"))
        else:
            out.append(it)
    return out


def _set_cols(sql: str) -> list[str]:
    m = re.search(r"UPDATE bom_header SET (.*?) WHERE", sql, re.S)
    assert m, sql
    return [part.split("=")[0].strip() for part in m.group(1).split(",")]


# ── Registration + auth floor ────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_write_routes_are_registered_on_the_id_path(method):
    hits = [r for r in app.routes
            if getattr(r, "path", None) == f"{BASE}/{{bom_id}}" and method in r.methods]
    assert len(hits) == 1, f"{method} {BASE}/{{bom_id}} not registered exactly once"
    assert hits[0].tags == ["BOM - Core"]


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_write_routes_require_authentication(method):
    res = TestClient(app).request(method.upper(), BOM, json={"notes": "x"})
    assert res.status_code in (401, 403)


def _viewer_conn_cls(base):
    class ViewerConn(base):
        async def fetch(self, sql, *args):
            self.calls.append((sql, args))
            # Grant ONLY 'view'. If either gate were on action="view" the
            # request would pass, so a 403 proves the action string.
            if len(args) >= 5 and args[1] == "bom" and args[4] == "view":
                return [{"allowed_entities": None, "allowed_warehouses": None,
                         "allowed_floors": None}]
            return []
    return ViewerConn


def test_update_is_denied_for_a_role_holding_only_bom_view(client_with):
    conn = _viewer_conn_cls(UpdateConn)()
    res = client_with(conn, _session(is_admin=False, role_name="viewer")).patch(
        BOM, json={"notes": "x"})
    assert res.status_code == 403, res.text
    assert conn.writes() == []


def test_delete_is_denied_for_a_role_holding_only_bom_view(client_with):
    conn = _viewer_conn_cls(UpdateConn)()
    res = client_with(conn, _session(is_admin=False, role_name="viewer")).delete(BOM)
    assert res.status_code == 403, res.text
    assert conn.writes() == []


def test_update_allowed_when_the_role_holds_bom_update(client_with, monkeypatch):
    import app.modules.auth.services.permission_service as perm_svc

    async def _allow(*a, **k):
        return True

    monkeypatch.setattr(perm_svc, "check_permission", _allow)
    res = client_with(UpdateConn(), _session(is_admin=False)).patch(BOM, json={"notes": "x"})
    assert res.status_code == 200, res.text


# ═══════════════════════════════════════════════════════════════════════════
#  DELETE — soft
# ═══════════════════════════════════════════════════════════════════════════
def test_delete_marks_inactive_and_deletes_nothing(client_with):
    conn = UpdateConn()
    res = client_with(conn).delete(BOM)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {
        "bom_id": 411, "fg_sku_name": "ROASTED CASHEW 100G", "version": 2,
        "is_active": False, "already_inactive": False,
        "deactivated_by": SESSION["full_name"],
    }

    writes = conn.writes()
    assert len(writes) == 1, [w[0] for w in writes]
    sql, args = writes[0]
    assert sql == "UPDATE bom_header SET is_active = FALSE WHERE bom_id = $1"
    assert args == (411,)
    # The whole point: nothing is removed, and no child table is touched.
    assert conn.sql_of("DELETE") == []
    assert conn.sql_of("bom_line") == []
    assert conn.sql_of("bom_process_route") == []


def test_delete_locks_the_header_row_first(client_with):
    conn = UpdateConn()
    client_with(conn).delete(BOM)
    (lock,) = conn.sql_of("FOR UPDATE")
    assert lock[1] == (411,)
    assert conn.kinds().index(lock[0]) < conn.kinds().index(
        "UPDATE bom_header SET is_active = FALSE WHERE bom_id = $1")


def test_delete_is_idempotent_on_an_inactive_bom(client_with):
    conn = UpdateConn(header=dict(HEADER_ROW, is_active=False))
    res = client_with(conn).delete(BOM)

    assert res.status_code == 200, res.text
    assert res.json()["already_inactive"] is True
    assert res.json()["is_active"] is False
    assert conn.writes() == [], "an already-inactive BOM must not be rewritten"


def test_delete_404s_for_an_unknown_bom(client_with):
    conn = UpdateConn(header=None)
    res = client_with(conn).delete(f"{BASE}/999999")
    assert res.status_code == 404
    assert res.json()["error"] == "bom_not_found"
    assert res.json()["details"]["bom_id"] == 999999
    assert conn.writes() == []


def test_delete_runs_inside_a_transaction(client_with):
    conn = UpdateConn()
    client_with(conn).delete(BOM)
    kinds = conn.kinds()
    assert kinds.count("BEGIN") == 1 and kinds[-1] == "COMMIT"
    assert kinds.index("BEGIN") < kinds.index(
        "UPDATE bom_header SET is_active = FALSE WHERE bom_id = $1")


# ═══════════════════════════════════════════════════════════════════════════
#  PATCH — header
# ═══════════════════════════════════════════════════════════════════════════
def test_update_rejects_an_empty_body(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={})
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "empty_update"
    assert conn.calls == [], "no query may run for an empty update"


def test_update_404s_for_an_unknown_bom(client_with):
    conn = UpdateConn(header=None)
    res = client_with(conn).patch(f"{BASE}/999999", json={"notes": "x"})
    assert res.status_code == 404
    assert res.json()["error"] == "bom_not_found"
    assert conn.writes() == []


def test_update_sets_only_the_columns_that_were_sent(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={
        "notes": "re-checked", "pack_size_kg": 0.2, "effective_to": None,
        "floors": ["Floor 1", "Floor 2"], "shelf_life_days": 90,
    })
    assert res.status_code == 200, res.text

    (upd,) = conn.sql_of("UPDATE bom_header SET")
    cols = _set_cols(upd[0])
    # Emitted in WHITELIST order (deterministic), not payload order, and only
    # the five that were sent — nothing else is named in the SET.
    assert cols == ["pack_size_kg", "floors", "shelf_life_days", "effective_to", "notes"]
    # Bound in that same order, then bom_id last. `effective_to: null` really
    # binds NULL — a present key clears the column, an absent one leaves it.
    assert upd[1] == (0.2, ["Floor 1", "Floor 2"], 90, None, "re-checked", 411)
    assert "WHERE bom_id = $6" in upd[0]

    body = res.json()
    assert body["changes"]["header_fields"] == cols
    assert body["changes"]["lines"] is None and body["changes"]["route"] is None
    assert body["updated_by"] == SESSION["full_name"]
    assert body["version"] == 2 and body["is_active"] is True
    # Nothing was said about lines or route, so neither table is touched.
    assert conn.sql_of("INSERT INTO") == []
    assert conn.sql_of("DELETE FROM") == []


def test_update_returns_the_fresh_detail(client_with):
    conn = UpdateConn()
    body = client_with(conn).patch(BOM, json={"notes": "x"}).json()
    assert set(body["detail"]) == {"header", "lines", "route", "counts"}
    assert body["detail"]["header"]["bom_id"] == 411
    assert [l["line_number"] for l in body["detail"]["lines"]] == [1, 2, 3]
    assert body["detail"]["counts"]["step_count"] == 2
    # Read AFTER the write, inside the same transaction, so it sees the change.
    kinds = conn.kinds()
    assert kinds.index("UPDATE bom_header SET notes = $1 WHERE bom_id = $2") \
        < kinds.index("SELECT * FROM bom_line WHERE bom_id = $1 ORDER BY line_number ASC")
    assert kinds[-1] == "COMMIT"


def test_update_never_writes_version_bom_id_or_created_at(client_with):
    """Server-assigned identity cannot be edited, even if the body names it."""
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={
        "notes": "x", "version": 9, "bom_id": 1, "created_at": "2020-01-01T00:00:00Z",
    })
    assert res.status_code == 200, res.text
    (upd,) = conn.sql_of("UPDATE bom_header SET")
    assert _set_cols(upd[0]) == ["notes"]
    for forbidden in ("version", "bom_id =", "created_at"):
        assert forbidden not in upd[0].split("WHERE")[0]


def test_update_normalises_names_and_nulls_a_blank_customer(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={
        "fg_sku_name": "ROASTED  CASHEW 100G", "customer_name": "   "})
    assert res.status_code == 200, res.text
    (upd,) = conn.sql_of("UPDATE bom_header SET")
    assert upd[1] == ("ROASTED CASHEW 100G", None, 411)
    # Same name after folding -> not a rename -> no conflict probe.
    assert conn.sql_of("bom_id <> $2") == []


def test_update_rename_probes_for_another_active_bom_and_409s(client_with):
    conn = UpdateConn(clash={"bom_id": 77, "version": 1, "entity": "cfpl"})
    res = client_with(conn).patch(BOM, json={"fg_sku_name": "MALT BAR 40G"})

    assert res.status_code == 409, res.text
    assert res.json()["error"] == "bom_exists"
    assert res.json()["details"]["bom_id"] == 77

    (probe,) = conn.sql_of("bom_id <> $2")
    assert probe[1] == ("MALT BAR 40G", 411)
    # Folds the COLUMN too, and never ILIKE (see _NORM_FG_SQL).
    assert "chr(160)" in probe[0] and "regexp_replace" in probe[0]
    assert "ILIKE" not in probe[0]
    assert conn.writes() == []
    assert conn.kinds()[-1] == "ROLLBACK"


def test_update_rename_with_no_clash_proceeds(client_with):
    conn = UpdateConn(clash=None)
    res = client_with(conn).patch(BOM, json={"fg_sku_name": "MALT BAR 40G"})
    assert res.status_code == 200, res.text
    assert len(conn.sql_of("bom_id <> $2")) == 1
    assert res.json()["fg_sku_name"] == "MALT BAR 40G"


def test_update_reactivation_probes_and_409s_when_the_name_is_taken(client_with):
    conn = UpdateConn(header=dict(HEADER_ROW, is_active=False),
                      clash={"bom_id": 500, "version": 3, "entity": "cfpl"})
    res = client_with(conn).patch(BOM, json={"is_active": True})
    assert res.status_code == 409, res.text
    assert res.json()["error"] == "bom_exists"
    assert conn.writes() == []


def test_update_reactivation_when_free_flips_the_flag(client_with):
    conn = UpdateConn(header=dict(HEADER_ROW, is_active=False), clash=None)
    res = client_with(conn).patch(BOM, json={"is_active": True})
    assert res.status_code == 200, res.text
    (upd,) = conn.sql_of("UPDATE bom_header SET")
    assert upd[0] == "UPDATE bom_header SET is_active = $1 WHERE bom_id = $2"
    assert upd[1] == (True, 411)
    assert res.json()["is_active"] is True


def test_update_deactivation_via_patch_needs_no_probe(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"is_active": False})
    assert res.status_code == 200, res.text
    assert conn.sql_of("bom_id <> $2") == [], "going inactive cannot clash"
    assert res.json()["is_active"] is False


def test_update_maps_a_unique_violation_race_to_409(client_with):
    conn = UpdateConn(unique_on_update=True, clash=None)
    res = client_with(conn).patch(BOM, json={"fg_sku_name": "MALT BAR 40G"})
    assert res.status_code == 409, res.text
    assert res.json()["error"] == "bom_exists"
    assert conn.kinds()[-1] == "ROLLBACK"


@pytest.mark.parametrize("body, code", [
    ({"entity": "acme"}, "bad_entity"),
    ({"fg_sku_name": "   "}, "no_sku"),
    ({"fg_sku_name": None}, "no_sku"),
    ({"allowed_balance_tolerance_pct": None}, "bad_tolerance"),
    ({"allowed_balance_tolerance_pct": 2}, "bad_tolerance"),
    ({"lines": [_line(item_type="raw")]}, "bad_item_type"),
    ({"lines": [_line(quantity_per_unit=0)]}, "bad_qty"),
    ({"lines": [_line(quantity_per_unit=0.0004)]}, "qty_below_precision"),
    ({"lines": [_line(staging_method="teleport")]}, "bad_staging_method"),
    ({"route": [{"process_name": " "}]}, "no_process_name"),
])
def test_update_rejects_bad_values_with_400_and_writes_nothing(client_with, body, code):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json=body)
    assert res.status_code == 400, res.text
    assert res.json()["error"] == code
    assert conn.writes() == []


def test_update_rejects_an_empty_lines_list(client_with):
    """Pydantic min_length=1: a BOM with no lines is the broken state the
    browser exists to find, so PATCH cannot create one."""
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": []})
    assert res.status_code == 422, res.text
    assert conn.writes() == []


def test_update_accepts_a_valid_tolerance(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"allowed_balance_tolerance_pct": 0.002})
    assert res.status_code == 200, res.text
    (upd,) = conn.sql_of("UPDATE bom_header SET")
    assert upd[1] == (0.002, 411)


# ═══════════════════════════════════════════════════════════════════════════
#  PATCH — lines
# ═══════════════════════════════════════════════════════════════════════════
def test_update_lines_updates_kept_ids_inserts_new_and_deletes_missing(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": _ulines(
        9002,
        _line(material_sku_name="NEW SALT", item_type="rm", quantity_per_unit=0.01),
    )})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["changes"]["lines"] == {"inserted": 1, "updated": 1, "deleted": 2}
    assert body["changes"]["header_fields"] == []

    # 9001 and 9003 were not sent -> deleted, each scoped to this bom_id.
    dels = conn.sql_of("DELETE FROM bom_line ")
    assert [a for _, a in dels] == [(9001, 411), (9003, 411)]
    assert all("bom_id = $2" in s for s, _ in dels)

    # 9002 was sent -> UPDATE in place, keeping its id, now line 1.
    (upd,) = conn.sql_of("UPDATE bom_line SET line_number = $1")
    assert upd[1][0] == 1                       # line_number from array order
    assert upd[1][-2:] == (9002, 411)           # WHERE bom_line_id AND bom_id
    assert "KEPT 9002" in upd[1]

    # The new line is INSERTed as line 2.
    (ins,) = conn.sql_of("INSERT INTO bom_line")
    assert ins[1][:2] == (411, 2)
    assert "NEW SALT" in ins[1]

    # No header column was touched.
    assert conn.sql_of("UPDATE bom_header SET") == []


def test_update_lines_parks_line_numbers_before_renumbering(client_with):
    """Reordering swaps line_number values under UNIQUE(bom_id, line_number).
    The negative park frees the space so no intermediate state collides."""
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": _ulines(9003, 9001, 9002)})
    assert res.status_code == 200, res.text

    kinds = conn.kinds()
    park = "UPDATE bom_line SET line_number = -line_number WHERE bom_id = $1"
    assert park in kinds
    first_renumber = min(i for i, k in enumerate(kinds) if k.startswith("UPDATE bom_line SET line_number = $1"))
    assert kinds.index(park) < first_renumber
    assert [a[0] for _, a in conn.sql_of("UPDATE bom_line SET line_number = $1")] == [1, 2, 3]
    assert [a[-2] for _, a in conn.sql_of("UPDATE bom_line SET line_number = $1")] == [9003, 9001, 9002]
    assert conn.sql_of("DELETE FROM bom_line ") == []
    assert res.json()["changes"]["lines"] == {"inserted": 0, "updated": 3, "deleted": 0}


def test_update_lines_skips_the_park_when_every_line_is_new(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": [_line(), _line(material_sku_name="B")]})
    assert res.status_code == 200, res.text
    assert conn.sql_of("-line_number") == [], "nothing survives, nothing to park"
    assert len(conn.sql_of("DELETE FROM bom_line ")) == 3
    assert len(conn.sql_of("INSERT INTO bom_line")) == 2


def test_update_lines_rejects_an_id_from_another_bom(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": _ulines(9002, 4242)})
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "unknown_line"
    assert res.json()["details"]["bom_line_ids"] == [4242]
    assert conn.writes() == []
    assert conn.kinds()[-1] == "ROLLBACK"


def test_update_lines_rejects_a_duplicated_id(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": _ulines(9002, 9002)})
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "duplicate_line_id"
    assert res.json()["details"]["bom_line_ids"] == [9002]
    assert conn.writes() == []


def test_update_lines_409s_listing_every_referenced_line_and_rolls_back(client_with):
    """A job card points at 9001 and 9003. Both deletes are attempted (each in
    its own savepoint), both refusals are collected, and the one 409 names both
    — while the header change that preceded them is rolled back with it."""
    conn = UpdateConn(fk_blocked={9001, 9003})
    res = client_with(conn).patch(BOM, json={"notes": "trim", "lines": _ulines(9002)})

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["error"] == "line_referenced"
    assert body["details"]["bom_line_ids"] == [9001, 9003]
    assert len(body["details"]["references"]) == 2

    kinds = conn.kinds()
    # One savepoint per attempted delete, each rolled back to; the header
    # UPDATE happened before them; the OUTER transaction ends in ROLLBACK.
    assert kinds.count("SAVEPOINT") == 2
    assert kinds.count("ROLLBACK TO") == 2
    assert "UPDATE bom_header SET notes = $1 WHERE bom_id = $2" in kinds
    assert kinds[-1] == "ROLLBACK"
    # Nothing past the refusal: no park, no renumber, no insert.
    assert conn.sql_of("-line_number") == []
    assert conn.sql_of("UPDATE bom_line SET line_number = $1") == []
    assert conn.sql_of("INSERT INTO") == []


def test_update_lines_partial_fk_refusal_still_reports_only_the_blocked_ones(client_with):
    conn = UpdateConn(fk_blocked={9003})
    res = client_with(conn).patch(BOM, json={"lines": _ulines(9002)})
    assert res.status_code == 409, res.text
    assert res.json()["details"]["bom_line_ids"] == [9003]


def test_update_lines_warns_about_tally_and_duplicates(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": [
        _line(material_sku_name="CASHEW W240"),
        _line(material_sku_name="cashew  w240", item_type="pm"),
    ]})
    assert res.status_code == 200, res.text
    warnings = res.json()["warnings"]
    assert any("Tally" in w for w in warnings)
    assert any("more than one line" in w for w in warnings)


def test_update_without_lines_carries_no_line_warnings(client_with):
    conn = UpdateConn()
    assert client_with(conn).patch(BOM, json={"notes": "x"}).json()["warnings"] == []


def test_update_lines_round_trip_every_whitelisted_column_on_update_and_insert(client_with):
    full = dict(uom="kg", loss_pct=2.5, godown="RM Store", can_use_offgrade=True,
                offgrade_max_pct=10, unit_rate_inr=720.5, process_stage="Roast",
                staging_method="backflush", consumed_at_stage="Final FG (opening RM)")
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"lines": [
        _line(bom_line_id=9001, **full), _line(material_sku_name="NEW", **full)]})
    assert res.status_code == 200, res.text

    (upd,) = conn.sql_of("UPDATE bom_line SET line_number = $1")
    row = dict(zip(("line_number",) + tuple(svc._LINE_COLS) + ("bom_line_id", "bom_id"), upd[1]))
    for col in svc._LINE_COLS:
        assert col in row and f"{col} = $" in upd[0], col
    assert row["consumed_at_stage"] == "Final FG (opening RM)"
    assert row["can_use_offgrade"] is True and row["bom_line_id"] == 9001

    (ins,) = conn.sql_of("INSERT INTO bom_line")
    row = dict(zip(("bom_id", "line_number") + tuple(svc._LINE_COLS), ins[1]))
    assert row["staging_method"] == "backflush" and row["godown"] == "RM Store"


# ═══════════════════════════════════════════════════════════════════════════
#  PATCH — route
# ═══════════════════════════════════════════════════════════════════════════
def test_update_route_replaces_the_whole_strip(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"route": [
        {"process_name": "Sorting", "std_time_min": 20},
        {"process_name": "Metal Detection"},
    ]})
    assert res.status_code == 200, res.text
    assert res.json()["changes"]["route"] == {"steps_created": 2}

    kinds = conn.kinds()
    assert kinds.index("DELETE FROM bom_process_route WHERE bom_id = $1") \
        < kinds.index(svc._ROUTE_INSERT_SQL)
    ins = conn.sql_of("INSERT INTO bom_process_route")
    assert [a[:2] for _, a in ins] == [(411, 1), (411, 2)]
    # stage defaults to a slug of the process name.
    row = dict(zip(("bom_id", "step_number") + tuple(svc._ROUTE_COLS), ins[1][1]))
    assert row["stage"] == "metal_detection"
    # Lines untouched.
    assert conn.sql_of("bom_line") == [
        c for c in conn.calls if "FROM bom_line" in c[0]], "only the detail read"


def test_update_route_empty_list_clears_it(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={"route": []})
    assert res.status_code == 200, res.text
    assert res.json()["changes"]["route"] == {"steps_created": 0}
    assert len(conn.sql_of("DELETE FROM bom_process_route")) == 1
    assert conn.sql_of("INSERT INTO bom_process_route") == []


# ═══════════════════════════════════════════════════════════════════════════
#  Cross-cutting
# ═══════════════════════════════════════════════════════════════════════════
def test_update_runs_every_write_inside_one_transaction(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={
        "notes": "x", "lines": _ulines(9001, _line(material_sku_name="B")),
        "route": [{"process_name": "One"}]})
    assert res.status_code == 200, res.text
    kinds = conn.kinds()
    assert kinds.count("BEGIN") == 1 and kinds[-1] == "COMMIT"
    begin = kinds.index("BEGIN")
    assert all(i > begin for i, k in enumerate(kinds)
               if k.startswith(("INSERT INTO", "UPDATE ", "DELETE FROM")))


def test_every_generated_write_binds_exactly_as_many_args_as_placeholders(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={
        "notes": "x", "pack_size_kg": 0.5, "is_active": True,
        "lines": _ulines(9001, _line(material_sku_name="B")),
        "route": [{"process_name": "One"}, {"process_name": "Two"}]})
    assert res.status_code == 200, res.text
    writes = conn.writes()
    # header UPDATE, DELETE 9002 + 9003, park, UPDATE 9001, INSERT B,
    # DELETE route, INSERT One + Two.
    assert len(writes) == 9, [w[0][:50] for w in writes]
    for sql, args in writes:
        nums = {int(n) for n in re.findall(r"\$(\d+)", sql)}
        assert nums == set(range(1, len(args) + 1)), \
            f"placeholders {sorted(nums)} vs {len(args)} args in {sql[:80]}"


@requires_pglast
def test_every_statement_the_update_path_emits_is_valid_postgres(client_with):
    conn = UpdateConn()
    res = client_with(conn).patch(BOM, json={
        "notes": "x", "fg_sku_name": "RENAMED", "is_active": True,
        "lines": _ulines(9001, _line(material_sku_name="B")),
        "route": [{"process_name": "One"}]})
    assert res.status_code == 200, res.text
    for sql, _ in conn.calls:
        if sql in ("BEGIN", "COMMIT", "SAVEPOINT", "RELEASE"):
            continue
        pglast.parse_sql(sql)


@requires_pglast
def test_every_statement_the_delete_path_emits_is_valid_postgres(client_with):
    conn = UpdateConn()
    client_with(conn).delete(BOM)
    for sql, _ in conn.calls:
        if sql in ("BEGIN", "COMMIT"):
            continue
        pglast.parse_sql(sql)


# ── Service-level, no HTTP ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_service_update_returns_none_for_a_missing_bom():
    assert await svc.update_bom(UpdateConn(header=None), 1, {"notes": "x"}) is None


@pytest.mark.asyncio
async def test_service_deactivate_returns_none_for_a_missing_bom():
    assert await svc.deactivate_bom(UpdateConn(header=None), 1) is None


def test_update_whitelist_is_create_whitelist_plus_tolerance_and_is_active():
    assert set(svc._HEADER_UPDATE_COLS) == set(svc._HEADER_COLS) | {
        "allowed_balance_tolerance_pct", "is_active"}
    for never in ("bom_id", "version", "created_at"):
        assert never not in svc._HEADER_UPDATE_COLS


# ═══════════════════════════════════════════════════════════════════════════
#  139 migration
# ═══════════════════════════════════════════════════════════════════════════
_MIG = Path(__file__).parents[2] / "app" / "db" / "139_bom_module_rbac_update_delete.sql"


@requires_pglast
def test_rbac_139_is_valid_postgres_and_null_safe():
    sql = _MIG.read_text(encoding="utf-8")
    stmts = pglast.parse_sql(sql)
    assert len(stmts) == 2, "one catalog seed + one admin grant"
    assert "NOT EXISTS" in sql and "IS NOT DISTINCT FROM" in sql
    assert "ON CONFLICT" in sql


def test_rbac_139_seeds_update_and_delete_only():
    sql = _MIG.read_text(encoding="utf-8")
    body = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
    assert "'update'" in body and "'delete'" in body
    # view/create belong to 095; re-seeding them here would be a duplicate
    # source of truth for the same tuples.
    assert "NULL::text, 'view'" not in body and "NULL::text, 'create'" not in body


def test_rbac_139_grants_to_admin_only():
    sql = _MIG.read_text(encoding="utf-8")
    granted = set(re.findall(r"r\.role_name\s*=\s*'([^']+)'", sql))
    for m in re.finditer(r"r\.role_name\s+IN\s*\(([^)]*)\)", sql):
        granted.update(re.findall(r"'([^']+)'", m.group(1)))
    assert granted == {"admin"}


def test_rbac_139_is_registered_in_the_runner_after_138():
    runner = (Path(__file__).parents[2] / "scripts" / "migrate.py").read_text(encoding="utf-8")
    assert '"139_bom_module_rbac_update_delete.sql"' in runner
    assert runner.index('"138_debtors_collection_full_access.sql"') \
        < runner.index('"139_bom_module_rbac_update_delete.sql"')
