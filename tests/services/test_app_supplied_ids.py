"""Tables whose primary key lost its DEFAULT in migration 019 need the app to supply
the id on every INSERT — otherwise the INSERT fails NOT NULL and rolls back the
caller's whole transaction (so_revision_log_v2 had 0 rows on RDS: every Revise,
Cancel, Carry-forward, BOM override and live job-card qty edit was failing).
"""
import asyncio
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "app" / "db" / "019_v2_app_supplied_ids.sql"


def _app_supplied_id_columns() -> dict[str, str]:
    sql = MIGRATION.read_text(encoding="utf-8")
    pairs = re.findall(r"ALTER\s+TABLE\s+(\w+)\s+ALTER\s+COLUMN\s+(\w+)\s+DROP\s+DEFAULT", sql, re.I)
    assert pairs, "migration 019 no longer lists any DROP DEFAULT - update this test"
    return {t.lower(): c.lower() for t, c in pairs}


def test_every_insert_into_an_app_supplied_id_table_supplies_the_id():
    ids = _app_supplied_id_columns()
    assert ids.get("so_revision_log_v2") == "revision_id"
    missing = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)", text, re.I | re.S):
            table, cols = m.group(1).lower(), m.group(2)
            if table not in ids or "{" in cols:  # dynamic column list: can't check statically
                continue
            if not re.search(rf"\b{ids[table]}\b", cols, re.I):
                line = text[: m.start()].count("\n") + 1
                missing.append(f"{path.relative_to(ROOT)}:{line} INSERT INTO {table} without {ids[table]}")
    assert not missing, "\n".join(missing)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, row):
        self.row = dict(row)
        self.executed: list[tuple[str, tuple]] = []

    def is_in_transaction(self):
        return True

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        return dict(self.row)

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "INSERT 0 1" if sql.lstrip().upper().startswith("INSERT") else "UPDATE 1"


def test_revise_order_logs_each_change_with_an_app_supplied_revision_id(monkeypatch):
    from app.modules.production.services import fulfillment_v2

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(fulfillment_v2.events, "fulfillment_revised", _noop)
    conn = _FakeConn({
        "so_fulfillment_id": 33644281, "entity": "cfpl",
        "original_qty_kg": 0, "dispatched_qty_kg": 0,
        "original_qty_units": 2900, "dispatched_qty_units": 0,
        "deadline_date": None,
    })
    out = asyncio.run(fulfillment_v2.revise_order(conn, 33644281, new_qty=696.0, reason="r", revised_by="u"))
    assert out["revised"] is True
    logs = [(sql, args) for sql, args in conn.executed if "INSERT INTO so_revision_log_v2" in sql]
    assert len(logs) == 1
    sql, args = logs[0]
    assert "revision_id" in sql
    assert isinstance(args[0], int) and 0 < args[0] < 10**8  # new_short_time_id()
    assert args[1:4] == (33644281, "qty_change", "0.0")
