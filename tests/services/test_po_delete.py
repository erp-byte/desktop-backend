"""PO delete on a database without the grn / dock_arrival tables.

Neither table exists in the current schema. soft_delete used to COUNT(*) grn
unguarded, so every delete raised UndefinedTableError and returned HTTP 500.
Catching that error is not enough either: the count runs inside the delete's
transaction, and Postgres refuses every statement after a failed one. The fake
connection below behaves the same way, so the tests fail on either mistake.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app.modules.purchase.services import po_delete

TXN = "TR-20261019103000"


class FakeConn:
    def __init__(self, *, tables=("po_box",), counts=None):
        self.tables = set(tables)
        self.counts = {"po_box": 0, "grn": 0, "dock_arrival": 0, **(counts or {})}
        self.aborted = False
        self.sql: list[str] = []

    def _run(self, sql):
        if self.aborted:
            raise asyncpg.InFailedSQLTransactionError(
                "current transaction is aborted, commands ignored until end of transaction block")
        s = " ".join(sql.split())
        self.sql.append(s)
        return s

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql, *args):
        self._run(sql)
        return {"transaction_no": TXN, "entity": "cfpl", "po_number": "PO-1", "deleted_at": None}

    async def fetchval(self, sql, *args):
        s = self._run(sql)
        if s.startswith("SELECT to_regclass"):
            name = args[0].removeprefix("public.")
            return name if name in self.tables else None
        table = s.split(" FROM ", 1)[1].split()[0]
        if table not in self.tables:
            self.aborted = True
            raise asyncpg.UndefinedTableError(f'relation "{table}" does not exist')
        return self.counts[table]

    async def execute(self, sql, *args):
        self._run(sql)


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture(autouse=True)
def _no_event_log(monkeypatch):
    async def write(conn, **kwargs):
        conn.sql.append("event_log")
    monkeypatch.setattr(po_delete.po_event_log, "write", write)


def _delete(conn, **over):
    kwargs = {"transaction_no": TXN, "reason": "uploaded twice", "actor_user_id": "u1",
              "actor_role": "purchase_manager", "actor_is_admin": False, **over}
    return asyncio.run(po_delete.soft_delete(FakePool(conn), **kwargs))


def test_delete_works_without_grn_and_dock_arrival_tables():
    conn = FakeConn()
    out = _delete(conn)
    assert out["dependent_records"] == {"dock_arrivals": 0, "grns": 0, "po_boxes": 0}
    assert any(s.startswith("UPDATE po_header SET deleted_at") for s in conn.sql)
    assert "event_log" in conn.sql
    # The missing tables are never queried, so the transaction stays usable.
    assert not any("FROM grn" in s or "FROM dock_arrival" in s for s in conn.sql)


def test_grn_and_dock_arrival_are_counted_once_they_exist():
    conn = FakeConn(tables=("po_box", "grn", "dock_arrival"), counts={"grn": 2, "dock_arrival": 1})
    out = _delete(conn)
    assert out["dependent_records"] == {"dock_arrivals": 1, "grns": 2, "po_boxes": 0}


def test_weighed_po_still_needs_store_head_or_admin():
    conn = FakeConn(counts={"po_box": 3})
    with pytest.raises(Exception) as err:
        _delete(conn)
    assert getattr(err.value, "status_code", None) == 403
    assert not any(s.startswith("UPDATE po_header") for s in conn.sql)


def test_admin_deletes_a_weighed_po():
    conn = FakeConn(counts={"po_box": 3})
    out = _delete(conn, actor_is_admin=True)
    assert out["dependent_records"]["po_boxes"] == 3
