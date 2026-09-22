"""Migration 116 (job_card_bom_change may hold FG and SFG articles; spec Addendum A).
Static checks of the files; the SQL is applied by the user, never by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_116 = ROOT / "app" / "db" / "116_job_card_bom_change_types.sql"
MIGRATE = ROOT / "scripts" / "migrate.py"


def _sql() -> str:
    return SQL_116.read_text(encoding="utf-8")


def _squash(s: str) -> str:
    """Whitespace-insensitive form: runs collapsed, no space inside parentheses."""
    s = re.sub(r"\s+", " ", s)
    return s.replace("( ", "(").replace(" )", ")")


def _statements(sql: str) -> list[str]:
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    return [l.strip() for l in body.splitlines() if l.strip()]


def test_registered_after_115_with_its_comment():
    text = MIGRATE.read_text(encoding="utf-8")
    i115 = text.index('"115_job_card_bom_change.sql"')
    i116 = text.index('"116_job_card_bom_change_types.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i115 < i116 < end
    between = text[text.index("\n", i115):i116].splitlines()
    comment = " ".join(l.strip().lstrip("#").strip() for l in between if l.strip().startswith("#"))
    assert comment == ("116 lets job_card_bom_change hold FG/SFG articles (accounted as RM, required qty "
                       "in kg). MUST follow 115. Idempotent.")


def test_whole_file_is_one_bounded_transaction():
    lines = _statements(_sql())
    assert lines[0] == "BEGIN;"
    assert lines[1] == "SET LOCAL lock_timeout = '5s';"
    assert lines[-1] == "COMMIT;"


def test_without_the_table_it_only_notices():
    s = _squash("\n".join(_statements(_sql())))
    assert ("IF to_regclass('public.job_card_bom_change') IS NULL THEN "
            "RAISE NOTICE 'job_card_bom_change absent -- apply 115 first'; RETURN; END IF;") in s


def test_the_four_alters_are_guarded_so_a_rerun_takes_no_lock():
    s = _squash("\n".join(_statements(_sql())))
    m = re.search(
        r"IF NOT EXISTS \(SELECT 1 FROM pg_constraint "
        r"WHERE conrelid = 'public\.job_card_bom_change'::regclass "
        r"AND conname = 'chk_jcbc_item_type' "
        r"AND pg_get_constraintdef\(oid\) LIKE '%sfg%'\) THEN (.+?) END IF; END \$\$;", s)
    assert m, "guarded block not found"
    block = m.group(1)
    assert block.count("ALTER TABLE job_card_bom_change") == 4
    assert block.index("DROP CONSTRAINT IF EXISTS chk_jcbc_item_type;") \
        < block.index("ADD CONSTRAINT chk_jcbc_item_type") \
        < block.index("DROP CONSTRAINT IF EXISTS chk_jcbc_required;") \
        < block.index("ADD CONSTRAINT chk_jcbc_required")
    outside = re.sub(r"DO \$\$.*?\$\$;", "", "\n".join(_statements(_sql())), flags=re.S)
    assert "ALTER" not in outside


def test_the_new_checks():
    s = _squash(_sql())
    assert "ADD CONSTRAINT chk_jcbc_item_type CHECK (item_type IN ('rm','pm','fg','sfg'));" in s
    assert ("ADD CONSTRAINT chk_jcbc_required CHECK (required_qty IS NULL OR ("
            "change_type = 'added' AND required_qty > 0 "
            "AND required_unit = CASE item_type WHEN 'pm' THEN 'pcs' ELSE 'kg' END "
            "AND (item_type <> 'pm' OR required_qty = trunc(required_qty))));") in s
    # The pair / sku / undone checks of 115 are left alone.
    assert "chk_jcbc_required_pair" not in s and "chk_jcbc_sku" not in s
