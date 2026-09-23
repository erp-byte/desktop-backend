"""Migration 117 (the store_head role and every grant written for it).
Static checks of the files; the SQL is applied by the user, never by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "app" / "db"
SQL_117 = DB / "117_store_head_role.sql"
MIGRATE = ROOT / "scripts" / "migrate.py"


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _body(sql: str) -> str:
    """The statements, without comment lines, whitespace collapsed."""
    lines = [l for l in sql.splitlines() if not l.strip().startswith("--")]
    return re.sub(r"\s+", " ", "\n".join(lines))


def test_it_creates_the_role_only_when_it_is_missing():
    body = _body(_sql(SQL_117))
    assert "INSERT INTO auth_role (role_name, description, is_admin)" in body
    assert "SELECT 'store_head', 'Stores', FALSE" in body
    # Guarded, not ON CONFLICT: re-running must not touch an existing row's
    # description (085 and 075 spell it differently).
    assert "WHERE NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'store_head')" in body


def test_every_grant_block_is_idempotent_and_scoped_to_the_role():
    body = _body(_sql(SQL_117))
    grants = body.count("INSERT INTO auth_role_permission")
    assert grants == 5, f"expected the five grant blocks, found {grants}"
    assert body.count("ON CONFLICT DO NOTHING") == grants
    assert body.count("r.role_name = 'store_head'") == grants


def test_it_grants_nothing_the_other_files_do_not():
    """Each block mirrors an existing file: purchase.po (005/087), material_in
    (075/085), receipt (006), ncr record read+approve (007), floor
    requisitions view/issue/cancel (111)."""
    body = _body(_sql(SQL_117))
    for module, sub in (("purchase", "po"), ("purchase", "material_in"), ("production", "floor_requisitions")):
        assert f"p.module = '{module}'" in body and f"p.sub_module = '{sub}'" in body
    assert "p.module = 'receipt'" in body
    assert "p.sub_module = 'record'" in body and "p.action IN ('read', 'approve')" in body
    assert "p.action IN ('view', 'issue', 'cancel')" in body
    # The floor-requisition permissions have no third level; 111 matches on that.
    assert "p.sub_sub_module IS NULL" in body


def test_the_grants_match_what_111_writes_for_the_role():
    eleven = _body(_sql(DB / "111_floor_requisition.sql"))
    for action in ("view", "issue", "cancel"):
        assert f"('store_head', '{action}')" in eleven
    assert "('store_head', 'create')" not in eleven, "117 must not grant create either"
    assert "('store_head', 'receive')" not in eleven, "the floor receives, not the store"


def test_one_transaction():
    sql = _sql(SQL_117)
    assert sql.lstrip().startswith("--")
    body = _body(sql)
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1
    assert "ROLLBACK" not in body


def test_it_is_registered_last_in_the_runner():
    text = _sql(MIGRATE)
    assert 'DB_DIR / "117_store_head_role.sql"' in text
    assert text.index('DB_DIR / "117_store_head_role.sql"') > text.index('DB_DIR / "116_job_card_bom_change_types.sql"')


def test_the_file_keeps_the_repo_line_endings():
    raw = SQL_117.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
