"""Migration 136 (the debtors_collection role, its permissions and grants).
Static checks of the files; the SQL is applied by the user, never by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_136 = ROOT / "app" / "db" / "136_debtors_collection_role.sql"
MIGRATE = ROOT / "scripts" / "migrate.py"

ACTIONS = ("view", "call", "edit", "export", "upload", "delete")


def _sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _body(sql: str) -> str:
    """The statements, without comment lines, whitespace collapsed."""
    lines = [l for l in sql.splitlines() if not l.strip().startswith("--")]
    return re.sub(r"\s+", " ", "\n".join(lines))


def _grant_blocks(body: str) -> list[str]:
    return [b for b in body.split("INSERT INTO ")[1:] if b.startswith("auth_role_permission")]


def test_it_creates_the_role_only_when_it_is_missing():
    body = _body(_sql(SQL_136))
    assert "INSERT INTO auth_role (role_name, description, is_admin) SELECT 'debtors_collection'," in body
    assert "FALSE WHERE NOT EXISTS (SELECT 1 FROM auth_role WHERE role_name = 'debtors_collection')" in body


def test_it_lists_the_six_module_level_permissions():
    body = _body(_sql(SQL_136))
    for action in ACTIONS:
        assert f"('debtors_collection', '{action}'," in body, action
    # Module-level: the web app reads them from /me as (module, sub_module NULL, action).
    assert "SELECT v.module, NULL, NULL, v.action, v.description" in body


def test_the_permissions_are_guarded_by_not_exists_not_on_conflict():
    """NULL sub_module / sub_sub_module are distinct in the unique index, so
    ON CONFLICT would add a duplicate row on every run."""
    body = _body(_sql(SQL_136))
    assert "ON CONFLICT" not in body
    assert body.count("WHERE NOT EXISTS") + body.count("AND NOT EXISTS") == 4


def test_the_role_gets_the_day_to_day_four_and_not_upload_or_delete():
    grants = _grant_blocks(_body(_sql(SQL_136)))
    assert len(grants) == 2, f"expected the role block and the admin block, found {len(grants)}"
    role = next(g for g in grants if "r.role_name = 'debtors_collection'" in g)
    assert "p.action IN ('view', 'call', 'edit', 'export')" in role
    assert "'upload'" not in role and "'delete'" not in role


def test_admin_roles_get_all_six():
    grants = _grant_blocks(_body(_sql(SQL_136)))
    admin = next(g for g in grants if "r.is_admin" in g)
    assert "p.module = 'debtors_collection'" in admin
    assert "p.action" not in admin, "the admin block must not narrow the actions"


def test_every_grant_is_module_level_and_idempotent():
    for g in _grant_blocks(_body(_sql(SQL_136))):
        assert "p.sub_module IS NULL" in g and "p.sub_sub_module IS NULL" in g
        assert "NOT EXISTS (SELECT 1 FROM auth_role_permission x" in g


def test_one_transaction():
    sql = _sql(SQL_136)
    assert sql.lstrip().startswith("--")
    body = _body(sql)
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1
    assert "ROLLBACK" not in body


def test_it_is_registered_last_in_the_runner():
    """Last in SQL_FILES itself; the optional legacy-drop step appends 054 after
    the list, and 133-135 may or may not be in the list yet, so neither counts."""
    text = _sql(MIGRATE)
    start = text.index("SQL_FILES = [")
    listing = text[start:text.index("\n]", start)]
    entries = re.findall(r'DB_DIR / "([^"]+\.sql)"', listing)
    assert entries[-1] == "136_debtors_collection_role.sql", entries[-3:]
    assert entries.count("136_debtors_collection_role.sql") == 1


def test_the_file_keeps_the_repo_line_endings():
    raw = SQL_136.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "app/db SQL files are CRLF"
