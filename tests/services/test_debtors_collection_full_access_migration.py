"""Migration 138 (the debtors_collection role gets the whole module).
Static checks of the files; the SQL is applied by the user, never by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_138 = ROOT / "app" / "db" / "138_debtors_collection_full_access.sql"
MIGRATE = ROOT / "scripts" / "migrate.py"


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _body(sql: str) -> str:
    """The statements, without comment lines, whitespace collapsed."""
    lines = [l for l in sql.splitlines() if not l.strip().startswith("--")]
    return re.sub(r"\s+", " ", "\n".join(lines))


def test_it_grants_every_module_level_permission_to_the_role():
    body = _body(_sql(SQL_138))
    assert "INSERT INTO auth_role_permission (role_id, permission_id)" in body
    assert "r.role_name = 'debtors_collection'" in body
    assert "p.module = 'debtors_collection'" in body
    assert "p.sub_module IS NULL" in body and "p.sub_sub_module IS NULL" in body
    # No action filter: that is what makes it the whole module.
    assert "p.action" not in body


def test_it_stays_inside_the_module():
    """Whole access to Debtors Collection, not admin: one grant, nothing else."""
    body = _body(_sql(SQL_138))
    assert body.count("INSERT INTO") == 1
    for word in ("UPDATE", "DELETE", "r.is_admin", "auth_user"):
        assert word not in body, word


def test_it_is_idempotent():
    body = _body(_sql(SQL_138))
    assert "ON CONFLICT" not in body
    assert "AND NOT EXISTS (SELECT 1 FROM auth_role_permission x" in body


def test_it_is_registered_once_in_the_runner_after_136():
    text = _sql(MIGRATE)
    start = text.index("SQL_FILES = [")
    listing = text[start:text.index("\n]", start)]
    entries = re.findall(r'DB_DIR / "([^"]+\.sql)"', listing)
    assert entries.count("138_debtors_collection_full_access.sql") == 1
    assert (entries.index("138_debtors_collection_full_access.sql")
            > entries.index("136_debtors_collection_role.sql")), entries[-4:]


def test_the_file_keeps_the_repo_line_endings():
    raw = SQL_138.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
