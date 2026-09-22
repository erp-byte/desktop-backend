"""Migration 115 (job_card_bom_change + returns index swap) and the 044 guard.
Static checks of the files; the SQL is applied by the user, never by tests."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SQL_115 = ROOT / "app" / "db" / "115_job_card_bom_change.sql"
SQL_044 = ROOT / "app" / "db" / "044_batch_aware_consumption_byproducts_indexes.sql"

INDEX_EXPR = ("(job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0), "
              "(CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END), "
              "balance_type)")


def _sql() -> str:
    return SQL_115.read_text(encoding="utf-8")


def _squash(s: str) -> str:
    """Whitespace-insensitive form: runs collapsed, no space inside parentheses."""
    s = re.sub(r"\s+", " ", s)
    return s.replace("( ", "(").replace(" )", ")")


def _statements(sql: str) -> list[str]:
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    return [l.strip() for l in body.splitlines() if l.strip()]


def test_registered_after_114():
    text = (ROOT / "scripts" / "migrate.py").read_text(encoding="utf-8")
    i114 = text.index('"114_floor_requisition_box_unique.sql"')
    i115 = text.index('"115_job_card_bom_change.sql"')
    end = text.index("# ── Optional: drop the v1 legacy")
    assert i114 < i115 < end


def test_whole_file_is_one_bounded_transaction():
    lines = _statements(_sql())
    assert lines[0] == "BEGIN;"
    assert lines[1] == "SET LOCAL lock_timeout = '5s';"
    assert lines[-1] == "COMMIT;"


def test_change_table_columns_and_checks():
    s = _squash(_sql())
    assert "CREATE TABLE IF NOT EXISTS job_card_bom_change" in s
    for col in ("change_id BIGINT PRIMARY KEY", "scope_job_card_id BIGINT NOT NULL",
                "plan_line_id BIGINT NOT NULL REFERENCES production_plan_line_v2 (plan_line_id) ON DELETE CASCADE",
                "change_type TEXT NOT NULL", "material_sku_name TEXT NOT NULL", "item_type TEXT NOT NULL",
                "sku_id INT", "required_qty NUMERIC(15,3)", "required_unit TEXT", "note TEXT",
                "made_on_job_card_id BIGINT NOT NULL", "made_on_job_card_number TEXT NOT NULL",
                "changed_by TEXT NOT NULL", "changed_at TIMESTAMPTZ NOT NULL DEFAULT now()",
                "undone_by TEXT", "undone_at TIMESTAMPTZ", "undo_reason TEXT"):
        assert col in s, col
    assert "CHECK (change_type IN ('removed','added'))" in s
    assert "CHECK (item_type IN ('rm','pm'))" in s
    assert "CHECK ((required_qty IS NULL) = (required_unit IS NULL))" in s
    assert "required_unit = CASE item_type WHEN 'rm' THEN 'kg' ELSE 'pcs' END" in s
    assert "(item_type = 'rm' OR required_qty = trunc(required_qty))" in s
    assert "CHECK ((change_type = 'added') = (sku_id IS NOT NULL))" in s
    assert "CHECK ((undone_at IS NULL) = (undone_by IS NULL))" in s


def test_one_live_change_per_article_per_job_card():
    s = _squash(_sql())
    assert ("CREATE UNIQUE INDEX IF NOT EXISTS uq_job_card_bom_change_live ON job_card_bom_change "
            "(scope_job_card_id, UPPER(BTRIM(material_sku_name))) WHERE undone_at IS NULL") in s
    assert "CREATE INDEX IF NOT EXISTS idx_job_card_bom_change_line ON job_card_bom_change (plan_line_id)" in s


def test_swap_is_guarded_and_takes_the_strong_lock_first():
    s = _squash(_sql())
    m = re.search(
        r"IF to_regclass\('public\.uq_jcbm_v2_jc_batch_bom_type'\) IS NOT NULL THEN "
        r"LOCK TABLE job_card_balance_material_v2 IN ACCESS EXCLUSIVE MODE; "
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_line_type ON job_card_balance_material_v2 (.+?); "
        r"DROP INDEX uq_jcbm_v2_jc_batch_bom_type; "
        r"ELSIF to_regclass\('public\.uq_jcbm_v2_jc_batch_line_type'\) IS NULL THEN "
        r"CREATE UNIQUE INDEX uq_jcbm_v2_jc_batch_line_type ON job_card_balance_material_v2 (.+?); END IF;",
        s)
    assert m, "swap block not found"
    assert _squash(m.group(1)) == _squash(INDEX_EXPR)
    assert _squash(m.group(2)) == _squash(INDEX_EXPR)


def test_no_unguarded_index_on_the_returns_table():
    code = "\n".join(_statements(_sql()))                 # comments dropped
    outside = re.sub(r"DO \$\$.*?\$\$;", "", code, flags=re.S)
    assert "job_card_balance_material_v2" not in outside


def test_044_stands_down_once_the_new_index_exists():
    s = _squash(SQL_044.read_text(encoding="utf-8"))
    assert ("IF to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NULL THEN "
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_bom_type") in s
