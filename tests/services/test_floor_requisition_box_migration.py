"""Migration 113 — floor_requisition_box and sfg_box item_type 'rm'. A static check
of the file and its registration; the SQL is applied by the user, not by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = ROOT / "app" / "db" / "113_floor_requisition_box.sql"


def _sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def test_registered_after_112_in_the_runner():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i112 = text.index('"112_floor_requisition_store_response.sql"')
    i113 = text.index('"113_floor_requisition_box.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i112 < i113 < end


def test_sfg_box_accepts_rm_and_keeps_sfg_and_fg():
    sql = _sql()
    assert "ALTER TABLE sfg_box DROP CONSTRAINT IF EXISTS chk_box_item_type;" in sql
    assert "CHECK (item_type IN ('sfg','fg','rm'))" in sql


def test_one_row_per_box_per_request():
    sql = _sql()
    assert "PRIMARY KEY (requisition_id, box_code)" in sql
    assert "REFERENCES floor_requisition (requisition_id)" in sql


def test_box_numbers_unique_per_request_and_required_when_printed():
    sql = _sql()
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_box_number\s+"
        r"ON floor_requisition_box \(requisition_id, box_number\) WHERE box_number IS NOT NULL",
        sql,
    )
    assert "CHECK (source IN ('printed','scanned'))" in sql
    assert "CHECK (source <> 'printed' OR box_number IS NOT NULL)" in sql


def test_weights_and_count_columns():
    sql = _sql()
    for col in ("net_weight", "gross_weight"):
        assert re.search(rf"\b{col}\s+NUMERIC\(15,3\)", sql), col
    assert re.search(r"\bcount\s+INT\b", sql)


def test_is_idempotent():
    sql = _sql()
    assert "CREATE TABLE IF NOT EXISTS floor_requisition_box" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_floor_requisition_box_code" in sql


# ── 114: one request per box ──
SQL_114 = ROOT / "app" / "db" / "114_floor_requisition_box_unique.sql"


def test_114_registered_after_113():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i113 = text.index('"113_floor_requisition_box.sql"')
    i114 = text.index('"114_floor_requisition_box_unique.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i113 < i114 < end


def test_114_makes_the_box_id_unique_across_requests():
    sql = SQL_114.read_text(encoding="utf-8")
    assert re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_box_code\s+"
                     r"ON floor_requisition_box \(box_code\);", sql)
    assert "DROP INDEX IF EXISTS idx_floor_requisition_box_code;" in sql


def test_114_says_how_to_find_boxes_already_on_two_requests():
    sql = SQL_114.read_text(encoding="utf-8")
    assert "HAVING count(*) > 1" in sql
