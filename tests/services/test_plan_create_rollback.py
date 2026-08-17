"""Regression: a rejected plan-create must leave NOTHING behind.

Observed on plan 63953387 (17 Aug 2026, CFPL / A-185): the operator's
POST /plans-v2 came back 400, yet the plan list still gained a draft plan
with 0 lines. An empty plan generates no job cards on approve, so the floor
never receives one — reported as "job cards are not getting dispatched".

Root cause: create_plan INSERTs the header first and only then resolves each
line's bom_id from bom_header. A missing BOM *returns* an {error} envelope
instead of raising, so the router's `async with conn.transaction()` block
exited cleanly and COMMITTED the orphan header (and any lines that already
succeeded, plus their so_fulfillment_v2 planned_qty bump); the 400 was raised
afterwards, outside the transaction. The fix moved that raise inside the
transaction block.

These tests call the real POST /plans-v2 handler over a fake connection that
models asyncpg commit / savepoint semantics, so the assertion can be on what
SURVIVES the transaction — something a real-DB rollback test can't observe
(it rolls everything back by construction), and something a hand-copied
mirror of the handler wouldn't catch if the handler drifted again.

Run:
    PYTHONPATH=. python tests/services/test_plan_create_rollback.py
    # or: pytest tests/services/test_plan_create_rollback.py
"""
import asyncio

from fastapi import HTTPException

from app.modules.production.router import PlanV2Create, create_plan_v2


class FakeConn:
    """asyncpg-shaped connection that tracks what a COMMIT would persist.

    `transaction()` nests: the outermost block commits its pending writes on
    clean exit and discards them on exception; inner blocks behave like
    SAVEPOINTs (rollback-to-mark on exception, no commit of their own). That
    is exactly the shape create_plan runs under — the router opens the outer
    transaction, insert_with_pk_retry opens a savepoint per row.
    """

    def __init__(self, *, boms: dict[str, int] | None = None):
        self.boms = boms or {}          # fg_sku_name → bom_id
        self.committed: list[tuple] = []   # ("table", id) rows that survived
        self._pending: list[tuple] = []
        self._depth = 0
        self._next_id = 1000

    # ── asyncpg surface ──────────────────────────────────────────────────
    def is_in_transaction(self) -> bool:
        return self._depth > 0

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                self._mark = len(conn._pending)
                conn._depth += 1
                return self

            async def __aexit__(self, exc_type, exc, tb):
                conn._depth -= 1
                if exc_type is not None:
                    del conn._pending[self._mark:]     # rollback (to savepoint)
                elif conn._depth == 0:
                    conn.committed.extend(conn._pending)   # COMMIT
                    conn._pending.clear()
                return False

        return _Tx()

    def _write(self, table: str) -> int:
        self._next_id += 1
        self._pending.append((table, self._next_id))
        return self._next_id

    async def fetchval(self, sql, *args):
        if "INSERT INTO production_plan_v2" in sql:
            return self._write("production_plan_v2")
        if "INSERT INTO production_plan_line_v2" in sql:
            return self._write("production_plan_line_v2")
        raise AssertionError(f"unexpected fetchval: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        if "FROM bom_header" in sql:
            bom_id = self.boms.get(args[0])
            return None if bom_id is None else {"bom_id": bom_id}
        raise AssertionError(f"unexpected fetchrow: {sql[:80]}")

    async def fetch(self, sql, *args):
        if "FROM bom_process_route" in sql:
            return []
        raise AssertionError(f"unexpected fetch: {sql[:80]}")

    async def execute(self, sql, *args):
        if "INSERT INTO production_plan_step_v2" in sql:
            self._write("production_plan_step_v2")
            return "INSERT 0 1"
        if "UPDATE so_fulfillment_v2" in sql:
            self._pending.append(("so_fulfillment_v2.planned_qty", args[2]))
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {sql[:80]}")


class FakePool:
    """`request.app.state.db_pool` — hands the router our FakeConn."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


class FakeUser:
    is_admin = True
    allowed_warehouses: list[str] = []
    full_name = "Dashrath Birajdar"
    phone = "0000000000"


class FakeRequest:
    def __init__(self, conn):
        self.app = type("app", (), {"state": type("state", (), {"db_pool": FakePool(conn)})})


def _body(*lines):
    return PlanV2Create(
        entity="cfpl",
        warehouse="A-185",
        plan_type="daily",
        plan_date="2026-08-17",
        date_from="2026-08-17",
        date_to="2026-08-20",
        lines=list(lines),
    )


def _line(sku, fid=87234561):
    return {
        "fg_sku_name": sku,
        "customer_name": "ACME FOODS",
        "planned_qty_kg": 500.0,
        "planned_qty_units": 2000,
        "linked_so_fulfillment_ids": [fid],
    }


def _post(conn, body):
    """Call the real POST /plans-v2 handler. Calling it directly bypasses
    FastAPI's Depends resolution, so the permission gate is supplied inline
    — everything else (transaction handling, error mapping) is the shipped
    code path, not a copy of it."""
    return asyncio.run(
        create_plan_v2(request=FakeRequest(conn), body=body, user=FakeUser()),
    )


def _expect_400(conn, body, code):
    try:
        _post(conn, body)
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
        assert exc.detail["error"] == code, exc.detail
        return exc
    raise AssertionError(f"expected a 400 {code}")


def _tables(conn):
    return [t for t, _ in conn.committed]


def test_missing_bom_leaves_no_orphan_plan():
    """The reported bug: 400 'no BOM' must not commit a 0-line draft plan."""
    conn = FakeConn(boms={})          # no active BOM for anything
    _expect_400(conn, _body(_line("SOME SKU 250g")), "no_bom")
    assert conn.committed == [], (
        f"rejected plan-create committed rows: {conn.committed} — the "
        "operator is left with an empty draft plan that can never produce "
        "job cards"
    )


def test_second_line_missing_bom_rolls_back_the_first():
    """Partial failure is worse: line 1 commits AND burns SO pending qty."""
    conn = FakeConn(boms={"HAS BOM 250g": 77})
    _expect_400(conn, _body(_line("HAS BOM 250g"), _line("NO BOM 500g")), "no_bom")
    assert conn.committed == [], (
        f"partial plan survived the rejection: {conn.committed}"
    )


def test_happy_path_still_commits():
    """Control: a fully-resolvable plan commits header + line as before."""
    conn = FakeConn(boms={"HAS BOM 250g": 77})
    result = _post(conn, _body(_line("HAS BOM 250g")))
    assert result.get("plan_id")
    assert len(result["plan_line_ids"]) == 1
    assert "production_plan_v2" in _tables(conn)
    assert "production_plan_line_v2" in _tables(conn)


def test_no_lines_is_rejected_before_any_write():
    conn = FakeConn()
    _expect_400(conn, _body(), "no_lines")
    assert conn.committed == []


if __name__ == "__main__":
    test_missing_bom_leaves_no_orphan_plan()
    test_second_line_missing_bom_rolls_back_the_first()
    test_happy_path_still_commits()
    test_no_lines_is_rejected_before_any_write()
    print("ASSERTIONS PASSED")
