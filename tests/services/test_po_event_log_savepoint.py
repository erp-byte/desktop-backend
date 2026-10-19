"""A failing PO audit insert must not take the PO down with it.

In Postgres a failed statement aborts the whole transaction, and that
transaction's COMMIT then silently rolls back. po_event_log.write runs inside the
transaction that inserts or deletes the PO, and on RDS its insert failed (the
table had an older shape). Catching the error was not enough: the PO's own
writes were discarded while the API reported success. The writer now takes a
savepoint, so a failed audit row rolls back alone.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.modules.purchase.services import po_event_log


class PgLikeConn:
    """Postgres semantics that matter here: a failed statement aborts the
    transaction until the enclosing savepoint is rolled back."""

    def __init__(self):
        self.aborted = False
        self.done: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        except Exception:
            self.aborted = False  # ROLLBACK TO SAVEPOINT
            raise

    async def execute(self, sql, *args):
        if self.aborted:
            raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")
        if "INSERT INTO po_event_log" in sql:
            self.aborted = True
            raise RuntimeError('column "actor_role" of relation "po_event_log" does not exist')
        self.done.append(sql.split()[2])  # the table written


def test_failed_audit_row_leaves_the_po_transaction_usable():
    async def run():
        conn = PgLikeConn()
        await conn.execute("INSERT INTO po_header (transaction_no) VALUES ($1)", "TR-20261019100000")
        await po_event_log.write(conn, transaction_no="TR-20261019100000", entity="cfpl",
                                 event_type=po_event_log.EVENT_CREATED, actor_user_id="7")
        assert not conn.aborted  # the PO's transaction can still commit
        await conn.execute("INSERT INTO po_line (transaction_no) VALUES ($1)", "TR-20261019100000")
        return conn.done

    assert asyncio.run(run()) == ["po_header", "po_line"]
