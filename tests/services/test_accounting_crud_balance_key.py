"""The record screen keys returns rows per article once migration 115's index exists (spec 2e)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.modules.production.services import jc_accounting_crud as crud

ROOT = Path(__file__).resolve().parents[2]


def _squash(s):
    return re.sub(r"\s+", " ", s).replace("( ", "(").replace(" )", ")")


def test_conflict_constant_matches_the_migration_index():
    sql = _squash((ROOT / "app" / "db" / "115_job_card_bom_change.sql").read_text(encoding="utf-8"))
    assert _squash(crud.BALANCE_LINE_TYPE_CONFLICT) in sql


def test_before_115_the_key_is_unchanged():
    a = {"bom_line_id": None, "balance_type": "returned", "material_name": "Salt"}
    b = {"bom_line_id": None, "balance_type": "returned", "material_name": "Sugar"}
    assert crud._line_key(crud._BALANCE, a) == crud._line_key(crud._BALANCE, b)
    bad = crud._validate_lines({"consumption": [], "byproducts": [], "additives": [],
                                "balance_materials": [a, b]})
    assert bad["error"] == "duplicate_line"


def test_after_115_rows_without_a_bom_line_are_keyed_by_article():
    a = {"bom_line_id": None, "balance_type": "returned", "material_name": " salt "}
    b = {"bom_line_id": None, "balance_type": "returned", "material_name": "Sugar"}
    c = {"bom_line_id": 7, "balance_type": "returned", "material_name": "Renamed"}
    assert crud._line_key(crud._BALANCE_115, a) == (None, "returned", "SALT")
    assert crud._line_key(crud._BALANCE_115, c) == (7, "returned", "")
    assert crud._validate_lines({"consumption": [], "byproducts": [], "additives": [],
                                 "balance_materials": [a, b]}, crud._BALANCE_115) is None
    assert crud._BALANCE_115["conflict"] == crud.BALANCE_LINE_TYPE_CONFLICT
    assert crud._BALANCE_115["key"] == crud._BALANCE["key"]          # INSERT column list unchanged


def test_balance_spec_follows_the_index():
    class Conn:
        def __init__(self, v):
            self.v = v

        async def fetchval(self, sql, *a):
            assert "uq_jcbm_v2_jc_batch_line_type" in sql
            return self.v
    assert asyncio.run(crud._balance_spec(Conn(True))) is crud._BALANCE_115
    assert asyncio.run(crud._balance_spec(Conn(False))) is crud._BALANCE


def test_write_checks_the_index_after_reading_the_table():
    import inspect
    src = inspect.getsource(crud._write)
    assert src.index("_fetch_sections(") < src.index("_balance_spec(") < src.index("_validate_lines(")
