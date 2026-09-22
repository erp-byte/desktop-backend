"""Migration 111 — floor_requisition. A static check of the file and its
registration; the SQL itself is applied to RDS by the user, not by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = ROOT / "app" / "db" / "111_floor_requisition.sql"


def _sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def test_registered_after_110_in_the_runner():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i110 = text.index('"110_stocktake_txn_verification.sql"')
    i111 = text.index('"111_floor_requisition.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i110 < i111 < end


def test_the_requisition_number_is_the_primary_key():
    assert re.search(r"requisition_id\s+BIGINT\s+PRIMARY KEY", _sql())


def test_every_quantity_is_stored_with_its_unit():
    sql = _sql()
    for q in ("requested", "required", "available", "shortage", "issued"):
        assert re.search(rf"\b{q}_qty\s+NUMERIC\(14,3\)", sql), q
        assert re.search(rf"\b{q}_unit\s+TEXT", sql), q
    assert "CHECK (requested_unit IN ('kg','pcs'))" in sql
    assert "CHECK (issued_unit   IS NULL OR issued_unit   = requested_unit)" in sql


def test_one_open_request_per_job_card_and_article():
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_floor_requisition_open\s+"
        r"ON floor_requisition \(job_card_id, UPPER\(BTRIM\(material_sku_name\)\)\)\s+"
        r"WHERE status = 'raised'",
        _sql(),
    )


def test_catalog_insert_is_null_safe():
    sql = _sql()
    assert "IS NOT DISTINCT FROM v.sub_module" in sql
    assert "p.sub_sub_module IS NULL" in sql


GRANTS = {
    "admin": {"view", "create", "issue", "receive", "cancel"},
    "floor_manager": {"view", "create", "receive", "cancel"},
    "store_head": {"view", "issue", "cancel"},
}


def test_grants_match_the_spec_and_nothing_more():
    found = set(re.findall(r"\('(admin|floor_manager|store_head)',\s*'(\w+)'\)", _sql()))
    want = {(role, action) for role, actions in GRANTS.items() for action in actions}
    assert found == want


# ── 112: store's Accept / Hold reply ─────────────────────────────────────────
SQL_112 = ROOT / "app" / "db" / "112_floor_requisition_store_response.sql"


def test_112_registered_right_after_111():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i111 = text.index('"111_floor_requisition.sql"')
    i112 = text.index('"112_floor_requisition_store_response.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i111 < i112 < end


def test_112_adds_three_columns_idempotently_with_a_guarded_check():
    sql = SQL_112.read_text(encoding="utf-8")
    for col, typ in (("store_response", "TEXT"), ("store_response_by", "TEXT"), ("store_response_at", "TIMESTAMPTZ")):
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {col}\s+{typ};", sql), col
    assert "conname  = 'ck_floor_requisition_store_response'" in sql and "IF NOT EXISTS" in sql
    assert "store_response IN ('accepted', 'on_hold')" in sql
