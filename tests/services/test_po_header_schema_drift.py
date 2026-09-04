"""Every po_header column the purchase module writes/filters must exist in SQL.

Regression origin: `_HEADER_COLUMNS` (po_commit) listed `supplier_id` and the
INSERT/UPDATE named it, but no CREATE TABLE or ALTER TABLE in app/db ever added
it. Every commit in a batch failed with:

    column "supplier_id" of relation "po_header" does not exist

Static + offline on purpose: it reads app/db/*.sql rather than a live database,
so it fails in CI on the drift itself instead of only on a deployed box.
"""
import re
from pathlib import Path

import pytest

from app.modules.purchase.services.po_commit import _HEADER_COLUMNS
from app.modules.purchase.services.po_query import (
    _EQUALITY_FIELDS,
    _SORTABLE_COLUMNS,
)

DB_DIR = Path(__file__).resolve().parents[2] / "app" / "db"

_CREATE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?po_header\s*\((.*?)\n\);", re.S | re.I)
_ALTER = re.compile(r"ALTER TABLE\s+po_header\b(.*?);", re.S | re.I)
_ADD_COL = re.compile(r"ADD COLUMN\s+(?:IF NOT EXISTS\s+)?([a-z_][a-z0-9_]*)", re.I)
_COL_DEF = re.compile(r"([a-z_][a-z0-9_]*)\s+[A-Z]")


def _po_header_columns() -> set[str]:
    cols: set[str] = set()
    for sql_file in DB_DIR.glob("*.sql"):
        text = sql_file.read_text(encoding="utf-8", errors="replace")
        for body in _CREATE.findall(text):
            for line in body.splitlines():
                line = line.strip()
                if line and not line.startswith("--"):
                    hit = _COL_DEF.match(line)
                    if hit:
                        cols.add(hit.group(1).lower())
        for body in _ALTER.findall(text):
            cols.update(m.lower() for m in _ADD_COL.findall(body))
    return cols


@pytest.fixture(scope="module")
def columns() -> set[str]:
    cols = _po_header_columns()
    assert "transaction_no" in cols, "parser found no po_header columns at all"
    return cols


@pytest.mark.parametrize("column", sorted(set(_HEADER_COLUMNS)))
def test_commit_writes_only_columns_that_exist(column, columns):
    assert column in columns, (
        f"po_commit writes po_header.{column} but no app/db/*.sql defines it — "
        f"every commit will fail with 'column \"{column}\" ... does not exist'"
    )


@pytest.mark.parametrize("column", sorted(set(_EQUALITY_FIELDS)))
def test_list_filters_only_on_columns_that_exist(column, columns):
    assert column in columns, f"po_query filters on po_header.{column}, which is not defined"


@pytest.mark.parametrize("column", sorted(_SORTABLE_COLUMNS))
def test_sort_keys_all_exist(column, columns):
    assert column in columns, f"po_query sorts on po_header.{column}, which is not defined"
