"""New POs get a plain TR-YYYYMMDDHHMMSS transaction number (IST), no suffix.

The commit used to mint TR-<time>-001, -002, … per batch (and an older build
TR-<time>-XXXX). Now each new PO takes the first free second, counting up
through a batch, and skips numbers already on a PO.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.purchase.services import po_commit

START = datetime(2026, 10, 19, 4, 30, 0, tzinfo=timezone.utc)  # 10:00:00 IST


class FakeConn:
    def __init__(self, existing=()):
        self.existing = set(existing)  # transaction numbers already on a PO
        self.sql: list[str] = []

    async def fetchval(self, sql, *args):
        return 1 if args and args[0] in self.existing else None

    async def fetchrow(self, sql, *args):
        return None  # no existing PO with this (entity, po_number)

    async def execute(self, sql, *args):
        self.sql.append(" ".join(sql.split()))

    @asynccontextmanager
    async def transaction(self):
        yield self


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def _mint(conn, taken=None, start=START):
    return asyncio.run(po_commit._mint_txn_no(conn, start, set() if taken is None else taken))


def test_plain_number_in_ist():
    assert _mint(FakeConn()) == "TR-20261019100000"


def test_skips_a_number_already_on_a_po():
    assert _mint(FakeConn(existing={"TR-20261019100000", "TR-20261019100001"})) == "TR-20261019100002"


def test_skips_a_number_minted_earlier_in_the_batch():
    taken = {"TR-20261019100000"}
    assert _mint(FakeConn(), taken) == "TR-20261019100001"
    assert taken == {"TR-20261019100000", "TR-20261019100001"}


def test_a_batch_commit_mints_one_second_apart_under_the_lock(monkeypatch):
    inserted = []

    async def insert_po(conn, *, transaction_no, **kw):
        inserted.append(transaction_no)

    async def write(conn, **kw):
        pass

    monkeypatch.setattr(po_commit, "_insert_po", insert_po)
    monkeypatch.setattr(po_commit.po_event_log, "write", write)
    conn = FakeConn()
    pos = [{"header": {"po_number": f"PO-{i}", "vendor_supplier_name": "V"}, "lines": [],
            "transaction_no": f"TXN-PREVIEW-20261019100000-00{i}"} for i in (1, 2, 3)]
    out = asyncio.run(po_commit.commit(FakePool(conn), entity="cfpl", mode="create_only", pos=pos,
                                       actor_user_id="u1", actor_role="purchase_manager"))
    assert out["errors"] == []
    assert out["created"] == inserted
    assert all(re.fullmatch(r"TR-\d{14}", t) for t in inserted)
    stamps = [datetime.strptime(t[3:], "%Y%m%d%H%M%S") for t in inserted]
    assert [b - a for a, b in zip(stamps, stamps[1:])] == [timedelta(seconds=1)] * 2
    assert conn.sql.count(" ".join(po_commit._MINT_LOCK_SQL.split())) == 3
