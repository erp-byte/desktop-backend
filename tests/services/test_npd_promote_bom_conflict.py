"""A unique violation on the promote INSERT must name the constraint that fired.

bom_header carries THREE unique indexes — bom_header_pkey (bom_id),
uq_bom_header_active_fg (fg_sku_name WHERE is_active) and uq_bom_header_fg_version
(fg_sku_name, version WHERE is_active) — but _mint_bom reported every one of them as:

    Couldn't promote — a live BOM for '<name>' already exists.
    Rename the target product or deactivate the existing BOM.

That advice is wrong for two of the three, and it is the LEAST likely of the three
to actually fire: _mint_bom deactivates the existing active BOM for this exact
fg_sku_name and bumps to the next version immediately before inserting, so the
FG-name indexes cannot collide on a single-threaded promote. The real-world cause
is bom_header_pkey — bom_header was bulk-loaded with explicit bom_ids and an import
that does not resync the sequence leaves nextval() handing out ids already taken,
so every promote fails until it catches up. Operators were told to rename a product
that was never the problem.

No DB: _mint_bom_recover is driven with synthetic asyncpg errors.

Run:  PYTHONPATH=. python -m pytest tests/services/test_npd_promote_bom_conflict.py
"""
from __future__ import annotations

import asyncio

import pytest
from asyncpg import exceptions as pg
from fastapi import HTTPException

from app.modules.sample.services import npd_dev_service as svc

NAME = "Macademia style 1"


def _violation(constraint: str) -> pg.UniqueViolationError:
    return pg.UniqueViolationError.new(
        {"C": "23505", "M": f'duplicate key value violates unique constraint "{constraint}"',
         "n": constraint, "t": "bom_header"})


class _Conn:
    """Minimal asyncpg-connection stand-in.

    `insert_results` is consumed one entry per _insert_bom_header attempt: an int is
    returned as the new bom_id, an exception is raised.
    """

    def __init__(self, *insert_results):
        self.pending = list(insert_results)
        self.executed: list[str] = []

    async def fetchval(self, query, *args):
        if "INSERT INTO bom_header" in query:
            nxt = self.pending.pop(0)
            if isinstance(nxt, BaseException):
                raise nxt
            return nxt
        raise AssertionError(f"unexpected fetchval: {query[:60]}")

    async def execute(self, query, *args):
        self.executed.append(" ".join(query.split()))

    def transaction(self):
        conn = self

        class _Savepoint:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False        # never swallow — the caller handles the error

        return _Savepoint()

    @property
    def resynced_sequence(self) -> bool:
        return any("setval" in q and "bom_header" in q for q in self.executed)


def _recover(conn, err):
    return asyncio.run(svc._mint_bom_recover(conn, err, NAME, 3, "note"))


# ── bom_header_pkey: the real-world cause — repair and carry on ──────────────

def test_pkey_violation_resyncs_the_sequence_and_retries():
    conn = _Conn(4242)                       # the retry succeeds
    assert _recover(conn, _violation("bom_header_pkey")) == 4242
    assert conn.resynced_sequence, "the id sequence was never resynced"


def test_pkey_violation_does_not_blame_the_product_name():
    """The bug report: an operator told to rename 'Macademia style 1' when the
    product name had nothing to do with it."""
    conn = _Conn(_violation("bom_header_pkey"))   # retry fails too
    with pytest.raises(HTTPException) as ei:
        _recover(conn, _violation("bom_header_pkey"))
    detail = ei.value.detail
    assert detail["error"] == "bom_id_sequence"
    assert "Rename the target product" not in detail["message"]
    assert "sequence" in detail["message"].lower()
    assert "setval" in detail["message"], "the message should say how to repair it"


# ── the FG-name indexes: the rename advice IS right ──────────────────────────

@pytest.mark.parametrize("constraint",
                         ["uq_bom_header_active_fg", "uq_bom_header_fg_version"])
def test_fg_name_violation_keeps_the_rename_advice(constraint):
    with pytest.raises(HTTPException) as ei:
        _recover(_Conn(), _violation(constraint))
    detail = ei.value.detail
    assert detail["error"] == "bom_conflict"
    assert NAME in detail["message"]
    assert "Rename the target product" in detail["message"]
    assert detail["details"]["constraint"] == constraint


# ── anything else: name it, don't guess ──────────────────────────────────────

def test_unknown_constraint_is_reported_verbatim():
    with pytest.raises(HTTPException) as ei:
        _recover(_Conn(), _violation("some_future_index"))
    detail = ei.value.detail
    assert "some_future_index" in detail["message"]
    assert "Rename the target product" not in detail["message"]


def test_missing_constraint_name_still_produces_a_409():
    """Older servers / pooled proxies can omit the constraint field."""
    with pytest.raises(HTTPException) as ei:
        _recover(_Conn(), pg.UniqueViolationError.new({"C": "23505", "M": "dup"}))
    assert ei.value.status_code == 409
    assert "unknown" in ei.value.detail["message"]


# ── every path answers 409, never a 500 ──────────────────────────────────────

@pytest.mark.parametrize("constraint", [
    "uq_bom_header_active_fg", "uq_bom_header_fg_version", "whatever"])
def test_conflicts_surface_as_409(constraint):
    with pytest.raises(HTTPException) as ei:
        _recover(_Conn(), _violation(constraint))
    assert ei.value.status_code == 409
