# Per-job-card BOM changes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user remove an RM/PM article from, or add one to, ONE job card's BOM (every stage card of its chain) from the Material allocation tab, without writing the BOM module, and make every reader of the job card's BOM honour it.

**Architecture:** A new table `job_card_bom_change` (migration 115) keyed on the chain's first card. One server module, `jc_bom_changes.py`, owns scope, the effective BOM, the article checks and the writes. `get_job_card`, Save Output, extra giveaway, requisitions, receive-material, the PDF and the rebuild path call it. The web reads the effective `bom_lines` plus a `bom_changes` block, edits via two endpoints, and makes Accounting's "clear a figure" really save 0.

**Tech Stack:** FastAPI + asyncpg (server_replica, pytest with scripted fake connections), Next.js 16 / React 19 (web_replica, plain-Node `*.test.ts` files), PostgreSQL.

**Spec:** `server_replica/docs/superpowers/specs/2026-09-21-job-card-bom-changes-design.md` — read it first; this plan argues from it.

## Global Constraints

- Never write `bom_header` / `bom_line`. Never delete from `new_stock_entries`.
- Do not apply migrations to any database; do not connect to RDS. The user applies 044 + 115 (after deploying the new server).
- Do not commit. The user commits.
- Keep every existing file's line endings. Every file this plan edits is **CRLF** today (checked 2026-09-21): production `router.py`, `job_card_v2.py`, `jc_accounting_v2.py`, `jc_accounting_crud.py`, `job_card_pdf.py`, floor_requisition `requisition_service.py`, `scripts/migrate.py`, `app/db/044_…sql`, `tests/services/test_floor_requisition_service.py`, `tests/services/test_accounting_crud.py`, web `page.tsx`, `_MaterialAllocationTab.tsx`, `outputAccounting.ts`, `sample/_form.tsx`. After editing, verify with `grep -c $'\r$' FILE` vs `grep -vc $'\r$' FILE` (LF-only count must stay 0). New files may be LF.
- Article identity everywhere: `UPPER(BTRIM(name))` in SQL, `(name or "").strip().upper()` in Python, `articleKey()` (lib/floorStock.ts) on the web.
- Lock-order rule: any transaction that locks the plan line row (`production_plan_line_v2`) takes that lock **before any other row lock**.
- Server tests: `cd server_replica && .venv/Scripts/python -m pytest tests/services/<file> -q`. Full suite baseline: 1130 passed + 2 known failures in `test_sku_lookup_permission.py`.
- Web checks: `cd web_replica && node <file>.test.ts`; `npx tsc --noEmit -p . 2>&1 | grep -v "^\.next"`; `npx eslint <changed files>`. Never run `next build` (the dev server owns `.next`).
- The existing lint errors in `purchase/material-in/[transaction_no]/_SectionEditor.tsx` (react-hooks/set-state-in-effect) are pre-existing; ignore them.

## File map

| File | Responsibility |
|---|---|
| `server_replica/app/db/115_job_card_bom_change.sql` (new) | change table + returns index swap, one bounded transaction |
| `server_replica/app/db/044_batch_aware_consumption_byproducts_indexes.sql` | stop re-creating the old returns index once 115's exists |
| `server_replica/scripts/migrate.py` | register 115 after 114 |
| `server_replica/app/modules/production/services/jc_bom_changes.py` (new) | scope, changes, effective BOM, resolver, records check, writes, rebuild/merge helpers |
| `server_replica/app/modules/production/router.py` | two endpoints; Save Output lock + checks; model id coercion; receive-material; merge error mapping |
| `server_replica/app/modules/production/services/job_card_v2.py` | `get_job_card` bom_lines/bom_changes; consumption adoption; EGA; rebuild lock + re-point; merge refusal |
| `server_replica/app/modules/production/services/jc_accounting_v2.py` | off-grade adoption in `save_byproducts` |
| `server_replica/app/modules/production/services/jc_accounting_crud.py` | returns key + conflict switch on the 115 index |
| `server_replica/app/modules/floor_requisition/services/requisition_service.py` | lock, removed refusal, added articles |
| `server_replica/app/modules/production/services/job_card_pdf.py` | removed RM rows out, added RM rows in |
| `web_replica/src/lib/job-card-bom-rules.ts` (new) | pure types + helpers (node-testable) |
| `web_replica/src/lib/job-card-bom.ts` (new) | API calls + error class |
| `web_replica/src/app/modules/job-card/[id]/outputAccounting.ts` | key matching by article, zero-row seeding, zeroing helpers |
| `web_replica/src/app/modules/job-card/[id]/page.tsx` | types, fallback guard, seeded refs, clear-to-0, TabPanel props |
| `web_replica/src/app/modules/sample/_form.tsx` | ArticlePicker pins the picked name's type |
| `web_replica/src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx` | BOM card independent of floor stock, Actions column, dialogs, changes list |
| `web_replica/src/app/modules/job-card/[id]/_AddBomArticleDialog.tsx` (new) | Add article dialog |
| `web_replica/src/app/modules/job-card/[id]/_RemoveBomArticleDialog.tsx` (new) | ✕ confirmation dialog |
| `web_replica/src/app/modules/job-card/[id]/_BomChangesList.tsx` (new) | "Changes on this job card" list |

Tasks S1–S10 are the server track (sequential: they share files). Tasks W1–W5 are the web track (sequential). The two tracks are independent of each other except for the JSON shapes defined in S2/S3 (repeated in W1). Task V is the final verification.

---

### Task S1: Migration 115, the 044 guard, runner registration

**Files:**
- Create: `server_replica/app/db/115_job_card_bom_change.sql`
- Modify: `server_replica/app/db/044_batch_aware_consumption_byproducts_indexes.sql` (balance block, ~lines 153-156)
- Modify: `server_replica/scripts/migrate.py` (after the 114 entry, ~line 521)
- Test: `server_replica/tests/services/test_job_card_bom_change_migration.py` (new)

**Interfaces:**
- Produces: table `job_card_bom_change` (columns in the SQL below), index `uq_job_card_bom_change_live`, returns index `uq_jcbm_v2_jc_batch_line_type` whose expression is exactly
  `(job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0), (CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END), balance_type)` (S8 asserts its ON CONFLICT constant against this file).

- [ ] **Step 1: Write the failing test** — `tests/services/test_job_card_bom_change_migration.py`:

```python
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
```

- [ ] **Step 2: Run it — expect FAIL** (`FileNotFoundError` for 115, 044 guard missing):
  `.venv/Scripts/python -m pytest tests/services/test_job_card_bom_change_migration.py -q`

- [ ] **Step 3: Create `app/db/115_job_card_bom_change.sql`** (any line ending; CRLF matches siblings):

```sql
-- ===========================================================================
-- 115_job_card_bom_change.sql
-- Per-job-card BOM changes. Spec:
-- docs/superpowers/specs/2026-09-21-job-card-bom-changes-design.md
--
-- 1. job_card_bom_change: an RM/PM article removed from, or added to, ONE job
--    card's BOM -- every stage card of its chain. scope_job_card_id is the
--    chain's first card (walk prev_job_card_id back while the plan line stays
--    the same). The BOM module's bom_line is never written. One live change per
--    article per job card; undoing keeps the row as history.
-- 2. Returns rows become unique per ARTICLE, not per missing BOM line:
--    uq_jcbm_v2_jc_batch_line_type replaces uq_jcbm_v2_jc_batch_bom_type and adds
--    UPPER(BTRIM(material_name)) for rows with no bom_line_id, so two added
--    articles can each have a 'returned' row in one batch. The new key is the old
--    one plus a column, so it cannot fail on data the old index accepted.
--
-- DEPLOY ORDER: deploy the server that understands this FIRST, then apply 115.
-- The old server's accounting record screen names the old index in ON CONFLICT
-- and fails once it is dropped. 044 was edited to stop re-creating the old index
-- once the new one exists (the runner re-runs every file on every deploy).
--
-- LOCKS: the whole file is one transaction with lock_timeout 5s. The swap takes
-- ACCESS EXCLUSIVE on job_card_balance_material_v2 first (no lock upgrade, no
-- deadlock) and only while the old index still exists, so a re-run takes no lock
-- on that table. CONCURRENTLY is impossible under this runner (see 092).
--
-- MUST follow 044, 092 and 111. Idempotent.
-- ===========================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS job_card_bom_change (
    change_id               BIGINT PRIMARY KEY,
    scope_job_card_id       BIGINT NOT NULL,
    plan_line_id            BIGINT NOT NULL REFERENCES production_plan_line_v2 (plan_line_id) ON DELETE CASCADE,
    change_type             TEXT   NOT NULL,
    material_sku_name       TEXT   NOT NULL,
    item_type               TEXT   NOT NULL,
    sku_id                  INT,
    required_qty            NUMERIC(15,3),
    required_unit           TEXT,
    note                    TEXT,
    made_on_job_card_id     BIGINT NOT NULL,
    made_on_job_card_number TEXT   NOT NULL,
    changed_by              TEXT   NOT NULL,
    changed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    undone_by               TEXT,
    undone_at               TIMESTAMPTZ,
    undo_reason             TEXT,
    CONSTRAINT chk_jcbc_type          CHECK (change_type IN ('removed','added')),
    CONSTRAINT chk_jcbc_item_type     CHECK (item_type IN ('rm','pm')),
    CONSTRAINT chk_jcbc_required_pair CHECK ((required_qty IS NULL) = (required_unit IS NULL)),
    CONSTRAINT chk_jcbc_required      CHECK (required_qty IS NULL OR (
        change_type = 'added' AND required_qty > 0
        AND required_unit = CASE item_type WHEN 'rm' THEN 'kg' ELSE 'pcs' END
        AND (item_type = 'rm' OR required_qty = trunc(required_qty)))),
    CONSTRAINT chk_jcbc_sku           CHECK ((change_type = 'added') = (sku_id IS NOT NULL)),
    CONSTRAINT chk_jcbc_undone        CHECK ((undone_at IS NULL) = (undone_by IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_job_card_bom_change_live
    ON job_card_bom_change (scope_job_card_id, UPPER(BTRIM(material_sku_name)))
    WHERE undone_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_job_card_bom_change_line
    ON job_card_bom_change (plan_line_id);

DO $$
BEGIN
    IF to_regclass('public.uq_jcbm_v2_jc_batch_bom_type') IS NOT NULL THEN
        LOCK TABLE job_card_balance_material_v2 IN ACCESS EXCLUSIVE MODE;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_line_type
            ON job_card_balance_material_v2
               (job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0),
                (CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END),
                balance_type);
        DROP INDEX uq_jcbm_v2_jc_batch_bom_type;
    ELSIF to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NULL THEN
        CREATE UNIQUE INDEX uq_jcbm_v2_jc_batch_line_type
            ON job_card_balance_material_v2
               (job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0),
                (CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END),
                balance_type);
    END IF;
END $$;

COMMIT;
```

- [ ] **Step 4: Edit 044's balance block** (keep CRLF). Replace:

```sql
    CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_bom_type
        ON job_card_balance_material_v2
           (job_card_id, COALESCE(batch_id, 0),
            COALESCE(bom_line_id, 0), balance_type);
END $$;
```

with:

```sql
    -- 115 replaces this index with uq_jcbm_v2_jc_batch_line_type (unique per
    -- article for rows with no bom_line_id). This file re-runs on every deploy,
    -- so it must not re-create the old, coarser index once the new one exists:
    -- rows the new index allows would make the rebuild fail and stop the runner.
    IF to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NULL THEN
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jcbm_v2_jc_batch_bom_type
            ON job_card_balance_material_v2
               (job_card_id, COALESCE(batch_id, 0),
                COALESCE(bom_line_id, 0), balance_type);
    END IF;
END $$;
```

- [ ] **Step 5: Register 115 in `scripts/migrate.py`** (keep CRLF), directly after the 114 entry:

```python
    # 115 creates job_card_bom_change (an RM/PM article removed from / added to ONE
    # job card's BOM, never the BOM module) and swaps the returns unique index to
    # uq_jcbm_v2_jc_batch_line_type (unique per article for rows with no bom_line_id).
    # DEPLOY THE SERVER FIRST: the old accounting record screen names the old index.
    # MUST follow 044 (which stands down once the new index exists), 092 and 111.
    # Idempotent.
    DB_DIR / "115_job_card_bom_change.sql",
```

- [ ] **Step 6: Run the test — expect PASS.** Also re-run the older migration tests: `.venv/Scripts/python -m pytest tests/services/test_job_card_bom_change_migration.py tests/services/test_floor_requisition_box_migration.py -q`
- [ ] **Step 7: Verify line endings** of 044 and migrate.py (LF-only count 0).

---

### Task S2: `jc_bom_changes` — read side (scope, changes, effective BOM, records)

**Files:**
- Create: `server_replica/app/modules/production/services/jc_bom_changes.py`
- Test: `server_replica/tests/services/test_jc_bom_changes.py` (new)

**Interfaces (produced; later tasks use exactly these names):**
- `article_key(name) -> str`, `loose_key(name) -> str`, `type_of(item_type) -> str`
- `class BomChangeError(Exception)`: `.http_status`, `.code`, `.message`, `.details`, `.detail() -> dict`
- `async table_exists(conn) -> bool`
- `@dataclass Scope(job_card_id, job_card_number, plan_line_id, bom_id, scope_job_card_id, cards)`; `.card_ids`, `.finished`
- `async scope_of(conn, job_card_id) -> Scope | None`
- `@dataclass Changes(removed: list[dict], added: list[dict])`; `.is_empty()`, `.by_key()`
- `async load_changes(conn, scope_job_card_id) -> Changes`
- `effective_bom_lines(master_lines, indent_lines, changes) -> tuple[list[dict], dict[str, set[int]]]`
- `changes_payload(scope_job_card_id, changes, flags) -> dict`
- `async detail_bom(conn, job_card_id, master_lines, rm_indents, pm_indents) -> tuple[list[dict], dict | None]`
- `async has_records(conn, scope, key, bom_line_ids) -> list[dict]`; `records_message(article, hits) -> str`
- `async lock_line(conn, plan_line_id, *, exclusive) -> None`; `async lock_line_for_card(conn, job_card_id) -> int | None`
- Every SQL constant starts with a `/* jcbc:<tag> */` comment; the tests' fake connection dispatches on the tag.

- [ ] **Step 1: Write the failing tests** — `tests/services/test_jc_bom_changes.py`:

```python
"""jc_bom_changes: scope, effective BOM, records check (spec 2a).
The fake connection answers by the /* jcbc:<tag> */ comment each query carries."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.modules.production.services import jc_bom_changes as m

TAG = re.compile(r"/\* (jcbc:[a-z_]+) \*/")


class FakeConn:
    """answers: {tag: value or callable(*args)}; records (tag, args) for every call."""

    def __init__(self, **answers):
        self.answers = {k.replace("__", ":"): v for k, v in answers.items()}
        self.calls: list[tuple[str, tuple]] = []

    def _answer(self, sql, args):
        t = TAG.search(sql)
        assert t, f"untagged query: {sql[:80]}"
        tag = t.group(1)
        self.calls.append((tag, args))
        assert tag in self.answers, f"unexpected query {tag}"
        v = self.answers[tag]
        return v(*args) if callable(v) else v

    async def fetch(self, sql, *args):
        return self._answer(sql, args)

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args)

    async def fetchval(self, sql, *args):
        return self._answer(sql, args)

    async def execute(self, sql, *args):
        return self._answer(sql, args)

    def tags(self):
        return [t for t, _ in self.calls]


def run(coro):
    return asyncio.run(coro)


WHEN = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def change(cid, ctype, name, itype="rm", **over):
    row = {"change_id": cid, "change_type": ctype, "material_sku_name": name, "item_type": itype,
           "sku_id": 11 if ctype == "added" else None, "required_qty": None, "required_unit": None,
           "note": None, "made_on_job_card_id": 1, "made_on_job_card_number": "PLAN-7-L1-S1",
           "changed_by": "Planner Pat", "changed_at": WHEN}
    row.update(over)
    return row


def line(bid, name, itype="rm", **over):
    row = {"bom_line_id": bid, "line_number": bid, "material_sku_name": name, "item_type": itype,
           "uom": "KGS", "quantity_per_unit": 0.5, "loss_pct": 1.0, "godown": None}
    row.update(over)
    return row


# ── pure helpers ──
def test_keys():
    assert m.article_key("  Sunflower Seeds ") == "SUNFLOWER SEEDS"
    assert m.article_key(None) == ""
    assert m.loose_key("Seeds  Roasted  100g") == "SEEDS ROASTED 100G"
    assert m.type_of(" PM ") == "pm"


def test_no_changes_returns_the_master_list_unchanged():
    master = [line(1, "Seeds"), line(2, "Pouch", "pm")]
    lines, flags = m.effective_bom_lines(master, [], m.Changes())
    assert lines == master and flags == {"not_on_bom": set(), "superseded": set()}


def test_removed_drops_every_line_with_that_article_and_added_is_appended():
    master = [line(1, "Seeds"), line(2, " seeds "), line(3, "Pouch", "pm")]
    ch = m.Changes(removed=[change(10, "removed", "SEEDS")],
                   added=[change(11, "added", "Salt", required_qty=Decimal("2.500"), required_unit="kg")])
    lines, flags = m.effective_bom_lines(master, [], ch)
    assert [l["material_sku_name"] for l in lines] == ["Pouch", "Salt"]
    salt = lines[1]
    assert salt == {"bom_line_id": None, "line_number": None, "material_sku_name": "Salt", "item_type": "rm",
                    "uom": "KGS", "quantity_per_unit": None, "loss_pct": None, "godown": None,
                    "added": True, "change_id": 11, "sku_id": 11, "required_qty": 2.5, "required_unit": "kg"}
    assert flags == {"not_on_bom": set(), "superseded": set()}


def test_superseded_and_not_on_bom_are_flagged():
    master = [line(1, "Seeds")]
    ch = m.Changes(removed=[change(10, "removed", "Old Pouch", "pm")], added=[change(11, "added", "seeds")])
    lines, flags = m.effective_bom_lines(master, [], ch)
    assert [l["material_sku_name"] for l in lines] == ["Seeds"]
    assert flags == {"not_on_bom": {10}, "superseded": {11}}


def test_an_empty_bom_with_changes_uses_the_indent_lines():
    indents = [{"bom_line_id": None, "material_sku_name": "Seeds", "item_type": "rm", "uom": "KGS",
                "loss_pct": Decimal("1.0"), "godown": "G1"}]
    ch = m.Changes(added=[change(11, "added", "Pouch", "pm")])
    lines, _ = m.effective_bom_lines([], indents, ch)
    assert [l["material_sku_name"] for l in lines] == ["Seeds", "Pouch"]
    assert lines[0]["loss_pct"] == 1.0 and lines[0]["quantity_per_unit"] is None


def test_changes_payload_shape():
    ch = m.Changes(removed=[change(10, "removed", "Seeds")],
                   added=[change(11, "added", "Pouch", "pm", required_qty=Decimal("100"), required_unit="pcs")])
    out = m.changes_payload(5, ch, {"not_on_bom": {10}, "superseded": set()})
    assert out == {
        "scope_job_card_id": 5,
        "removed": [{"change_id": 10, "material_sku_name": "Seeds", "item_type": "rm", "note": None,
                     "changed_by": "Planner Pat", "changed_at": WHEN.isoformat(),
                     "made_on_job_card_number": "PLAN-7-L1-S1", "not_on_bom": True}],
        "added": [{"change_id": 11, "material_sku_name": "Pouch", "item_type": "pm", "note": None,
                   "changed_by": "Planner Pat", "changed_at": WHEN.isoformat(),
                   "made_on_job_card_number": "PLAN-7-L1-S1", "sku_id": 11, "required_qty": 100.0,
                   "required_unit": "pcs", "superseded": False}],
    }


# ── scope ──
def test_scope_walks_back_to_the_first_card_and_lists_the_chains_live_cards():
    conn = FakeConn(
        jcbc__card={"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "plan_line_id": 70,
                    "bom_id": 9, "status": "locked"},
        jcbc__head=1,
        jcbc__chain=[{"job_card_id": 1, "job_card_number": "PLAN-7-L1-S1", "status": "completed"},
                     {"job_card_id": 2, "job_card_number": "PLAN-7-L1-S2", "status": "in_progress"},
                     {"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "status": "locked"}])
    s = run(m.scope_of(conn, 3))
    assert (s.scope_job_card_id, s.plan_line_id, s.bom_id, s.card_ids) == (1, 70, 9, [1, 2, 3])
    assert s.finished is False
    assert conn.calls[1] == ("jcbc:head", (3,)) and conn.calls[2] == ("jcbc:chain", (1,))


def test_a_deleted_card_has_no_scope():
    assert run(m.scope_of(FakeConn(jcbc__card=None), 3)) is None


def test_scope_sql_stays_on_one_plan_line():
    # Partial chains and merged runs: the walk must not cross plan lines.
    assert "p.plan_line_id = b.plan_line_id" in " ".join(m._HEAD_SQL.split())
    assert "c.plan_line_id = f.plan_line_id" in " ".join(m._CHAIN_SQL.split())


def test_finished_when_every_card_is_terminal():
    s = m.Scope(1, "X", 70, 9, 1, [{"job_card_id": 1, "job_card_number": "X", "status": "closed"},
                                   {"job_card_id": 2, "job_card_number": "Y", "status": "completed"}])
    assert s.finished is True


# ── changes / detail ──
def test_without_the_table_there_are_no_changes():
    conn = FakeConn(jcbc__table=False)
    assert run(m.load_changes(conn, 1)).is_empty()
    lines, payload = run(m.detail_bom(conn, 3, [line(1, "Seeds")], [], []))
    assert lines == [line(1, "Seeds")] and payload is None


def test_detail_bom_applies_the_chains_changes():
    conn = FakeConn(
        jcbc__table=True,
        jcbc__card={"job_card_id": 3, "job_card_number": "S3", "plan_line_id": 70, "bom_id": 9, "status": "locked"},
        jcbc__head=1,
        jcbc__chain=[{"job_card_id": 1, "job_card_number": "S1", "status": "unlocked"}],
        jcbc__changes=[change(10, "removed", "Seeds")])
    lines, payload = run(m.detail_bom(conn, 3, [line(1, "Seeds"), line(2, "Pouch", "pm")], [], []))
    assert [l["material_sku_name"] for l in lines] == ["Pouch"]
    assert payload["scope_job_card_id"] == 1 and payload["removed"][0]["change_id"] == 10
    assert ("jcbc:changes", (1,)) in conn.calls


# ── records ──
def test_has_records_passes_card_ids_key_and_line_ids():
    hits = [{"kind": "consumption", "qty": Decimal("12.5"), "job_card_id": 1, "job_card_number": "PLAN-7-L1-S1",
             "batch_id": 44, "batch_number": 2, "batch_status": "closed"}]
    conn = FakeConn(jcbc__records=hits)
    scope = m.Scope(1, "PLAN-7-L1-S1", 70, 9, 1, [{"job_card_id": 1, "job_card_number": "S1", "status": "in_progress"}])
    out = run(m.has_records(conn, scope, "SEEDS", [101]))
    assert conn.calls == [("jcbc:records", ([1], "SEEDS", [101]))]
    assert out == [{"kind": "consumption", "qty": 12.5, "job_card_id": 1, "job_card_number": "PLAN-7-L1-S1",
                    "batch_id": 44, "batch_number": 2, "batch_status": "closed"}]


def test_records_sql_ignores_zero_and_soft_deleted_rows_and_counts_issued():
    sql = " ".join(m._RECORDS_SQL.split())
    assert "m.deleted_at IS NULL AND m.actual_consumed_qty > 0" in sql
    assert "b.deleted_at IS NULL AND b.qty_kg > 0" in sql
    assert "y.deleted_at IS NULL AND y.quantity > 0" in sql
    assert "job_card_rm_indent_v2" in sql and "job_card_pm_indent_v2" in sql and "issued_qty > 0" in sql


def test_records_message_names_card_batch_and_what_to_do():
    msg = m.records_message("Seeds", [
        {"kind": "consumption", "qty": 12.5, "job_card_number": "PLAN-7-L1-S1", "batch_id": 44,
         "batch_number": 2, "batch_status": "open"},
        {"kind": "returned", "qty": 1.0, "job_card_number": "PLAN-7-L1-S1", "batch_id": 45,
         "batch_number": 3, "batch_status": "closed"},
        {"kind": "offgrade", "qty": 0.5, "job_card_number": "PLAN-7-L1-S1", "batch_id": None,
         "batch_number": None, "batch_status": None},
        {"kind": "issued", "qty": 12.0, "job_card_number": "PLAN-7-L1-S1", "batch_id": None,
         "batch_number": None, "batch_status": None},
    ])
    assert msg == (
        "Seeds can't be removed yet: consumption is saved on PLAN-7-L1-S1, batch 2; "
        "returned to store is saved on PLAN-7-L1-S1, batch 3 (closed: an admin must re-open it with the override); "
        "off-grade is saved on PLAN-7-L1-S1, saved without a batch; "
        "12 was received on PLAN-7-L1-S1 (return it to store first). "
        "Clear the figures in Accounting (clearing a figure saves 0), then remove it.")


# ── locks ──
def test_lock_sql():
    assert "FOR UPDATE" in m._LOCK_UPDATE_SQL and "production_plan_line_v2" in m._LOCK_UPDATE_SQL
    assert "FOR KEY SHARE" in m._LOCK_SHARE_SQL
    assert "FOR KEY SHARE OF l" in " ".join(m._LOCK_CARD_SHARE_SQL.split())


def test_lock_line_for_card_returns_the_plan_line():
    conn = FakeConn(jcbc__lock_card_share=70)
    assert run(m.lock_line_for_card(conn, 3)) == 70
    assert conn.calls == [("jcbc:lock_card_share", (3,))]
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError`).

- [ ] **Step 3: Create `app/modules/production/services/jc_bom_changes.py`:**

```python
"""Per-job-card BOM changes (migration 115).

A job card's BOM is the BOM module's bom_line rows for its bom_id -- read here,
never written. job_card_bom_change records what was removed from, or added to,
ONE job card: every stage card of its chain. scope_job_card_id is the chain's
first card, found by walking prev_job_card_id back while the plan line stays the
same (so partial chains -B2.. of one line, and a merged run's member packing
cards, are separate job cards). Everything that reads a job card's BOM goes
through here: get_job_card's bom_lines, Save Output, extra giveaway, floor
requisitions, receive-material, the PDF, the rebuild and merge paths.

Article identity is UPPER(BTRIM(name)), as in floor stock and requisitions.
Every query carries a /* jcbc:<tag> */ comment so tests can script a fake
connection by name.
Spec: docs/superpowers/specs/2026-09-21-job-card-bom-changes-design.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

import asyncpg

from app.core.helpers import insert_with_pk_retry, new_short_time_id

RM, PM = "rm", "pm"
ITEM_TYPES = (RM, PM)
UNIT_FOR = {RM: "kg", PM: "pcs"}
UOM_FOR = {RM: "KGS", PM: "PCS"}
FINISHED = ("completed", "closed", "cancelled")
EGA_CONSOLIDATED = "CONSOLIDATED"
LIVE_INDEX = "uq_job_card_bom_change_live"


class BomChangeError(Exception):
    """A refusal: the router answers HTTP `http_status` with detail()."""

    def __init__(self, http_status: int, code: str, message: str, **details: Any):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.details = details

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.details}


def article_key(name: Optional[str]) -> str:
    return (name or "").strip().upper()


_SPACES = re.compile(r"\s+")


def loose_key(name: Optional[str]) -> str:
    """article_key with runs of spaces / non-breaking spaces collapsed: only for
    the add-time 'already on the BOM' check, never as an identity."""
    return _SPACES.sub(" ", (name or "").replace(" ", " ")).strip().upper()


def type_of(item_type: Optional[str]) -> str:
    return (item_type or "").strip().lower()


def _json(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(r: Any) -> dict:
    return {k: _json(v) for k, v in dict(r).items()}


def _clean(text: Optional[str]) -> Optional[str]:
    t = (text or "").strip()
    return t[:500] or None


# ── SQL ─────────────────────────────────────────────────────────────────────

_TABLE_SQL = "/* jcbc:table */ SELECT to_regclass('public.job_card_bom_change') IS NOT NULL"

_CARD_SQL = """/* jcbc:card */
    SELECT job_card_id, job_card_number, plan_line_id, bom_id, status
      FROM job_card_v2
     WHERE job_card_id = $1 AND deleted_at IS NULL
"""

# Back through the previous stage while the plan line stays the same. Deleted
# cards are walked through (the chain's structure), the depth cap stops a cycle.
_HEAD_SQL = """/* jcbc:head */
    WITH RECURSIVE back AS (
        SELECT job_card_id, prev_job_card_id, plan_line_id, 0 AS depth
          FROM job_card_v2 WHERE job_card_id = $1
        UNION ALL
        SELECT p.job_card_id, p.prev_job_card_id, p.plan_line_id, b.depth + 1
          FROM back b
          JOIN job_card_v2 p ON p.job_card_id = b.prev_job_card_id
         WHERE p.plan_line_id = b.plan_line_id AND b.depth < 50
    )
    SELECT job_card_id FROM back ORDER BY depth DESC LIMIT 1
"""

_CHAIN_SQL = """/* jcbc:chain */
    WITH RECURSIVE fwd AS (
        SELECT job_card_id, plan_line_id, 0 AS depth FROM job_card_v2 WHERE job_card_id = $1
        UNION ALL
        SELECT c.job_card_id, c.plan_line_id, f.depth + 1
          FROM fwd f
          JOIN job_card_v2 c ON c.prev_job_card_id = f.job_card_id
         WHERE c.plan_line_id = f.plan_line_id AND f.depth < 50
    )
    SELECT j.job_card_id, j.job_card_number, j.status
      FROM fwd JOIN job_card_v2 j ON j.job_card_id = fwd.job_card_id
     WHERE j.deleted_at IS NULL
     ORDER BY fwd.depth, j.job_card_id
"""

_CHANGES_SQL = """/* jcbc:changes */
    SELECT change_id, change_type, material_sku_name, item_type, sku_id, required_qty,
           required_unit, note, made_on_job_card_id, made_on_job_card_number,
           changed_by, changed_at
      FROM job_card_bom_change
     WHERE scope_job_card_id = $1 AND undone_at IS NULL
     ORDER BY changed_at, change_id
"""

_MASTER_SQL = """/* jcbc:master */
    SELECT bom_line_id, line_number, material_sku_name, item_type,
           uom, quantity_per_unit, loss_pct, godown
      FROM bom_line
     WHERE bom_id = $1
     ORDER BY item_type, line_number
"""

_INDENTS_SQL = """/* jcbc:indents */
    SELECT bom_line_id, material_sku_name, 'rm' AS item_type, uom, loss_pct, godown
      FROM job_card_rm_indent_v2 WHERE job_card_id = $1
    UNION ALL
    SELECT bom_line_id, material_sku_name, 'pm', uom, loss_pct, godown
      FROM job_card_pm_indent_v2 WHERE job_card_id = $1
"""

_LOCK_UPDATE_SQL = """/* jcbc:lock_update */
    SELECT plan_line_id FROM production_plan_line_v2 WHERE plan_line_id = $1 FOR UPDATE
"""

_LOCK_SHARE_SQL = """/* jcbc:lock_share */
    SELECT plan_line_id FROM production_plan_line_v2 WHERE plan_line_id = $1 FOR KEY SHARE
"""

_LOCK_CARD_SHARE_SQL = """/* jcbc:lock_card_share */
    SELECT j.plan_line_id
      FROM job_card_v2 j
      JOIN production_plan_line_v2 l ON l.plan_line_id = j.plan_line_id
     WHERE j.job_card_id = $1
       FOR KEY SHARE OF l
"""

# Saved figures for one article on the chain's live cards. Zero rows and
# soft-deleted rows do not count. Issued indent qty is canonical input.
_RECORDS_SQL = """/* jcbc:records */
    WITH hits AS (
        SELECT 'consumption'::text AS kind, m.job_card_id, m.batch_id, m.actual_consumed_qty AS qty
          FROM job_card_material_consumption_v2 m
         WHERE m.job_card_id = ANY($1::bigint[])
           AND m.deleted_at IS NULL AND m.actual_consumed_qty > 0
           AND (UPPER(BTRIM(m.material_sku_name)) = $2 OR m.bom_line_id = ANY($3::int[]))
        UNION ALL
        SELECT b.balance_type, b.job_card_id, b.batch_id, b.qty_kg
          FROM job_card_balance_material_v2 b
         WHERE b.job_card_id = ANY($1::bigint[])
           AND b.deleted_at IS NULL AND b.qty_kg > 0
           AND (UPPER(BTRIM(b.material_name)) = $2 OR b.bom_line_id = ANY($3::int[]))
        UNION ALL
        SELECT 'offgrade', y.job_card_id, y.batch_id, y.quantity
          FROM job_card_byproducts_v2 y
         WHERE y.job_card_id = ANY($1::bigint[])
           AND y.deleted_at IS NULL AND y.quantity > 0
           AND (UPPER(BTRIM(y.material_name)) = $2 OR y.bom_line_id = ANY($3::int[]))
        UNION ALL
        SELECT 'issued', i.job_card_id, NULL::bigint, i.issued_qty
          FROM job_card_rm_indent_v2 i
         WHERE i.job_card_id = ANY($1::bigint[]) AND i.issued_qty > 0
           AND UPPER(BTRIM(i.material_sku_name)) = $2
        UNION ALL
        SELECT 'issued', i.job_card_id, NULL::bigint, i.issued_qty
          FROM job_card_pm_indent_v2 i
         WHERE i.job_card_id = ANY($1::bigint[]) AND i.issued_qty > 0
           AND UPPER(BTRIM(i.material_sku_name)) = $2
    )
    SELECT h.kind, h.qty, h.job_card_id, j.job_card_number, h.batch_id,
           bt.batch_number, bt.status AS batch_status
      FROM hits h
      JOIN job_card_v2 j ON j.job_card_id = h.job_card_id
      LEFT JOIN job_card_batch_v2 bt ON bt.batch_id = h.batch_id
     ORDER BY j.job_card_number, h.batch_id NULLS FIRST, h.kind
"""


# ── scope and changes ───────────────────────────────────────────────────────

async def table_exists(conn) -> bool:
    return bool(await conn.fetchval(_TABLE_SQL))


@dataclass
class Scope:
    job_card_id: int
    job_card_number: str
    plan_line_id: int
    bom_id: Optional[int]
    scope_job_card_id: int
    cards: list[dict]

    @property
    def card_ids(self) -> list[int]:
        return [c["job_card_id"] for c in self.cards]

    @property
    def finished(self) -> bool:
        return bool(self.cards) and all(c["status"] in FINISHED for c in self.cards)


async def scope_of(conn, job_card_id: int) -> Optional[Scope]:
    card = await conn.fetchrow(_CARD_SQL, job_card_id)
    if card is None:
        return None
    head = await conn.fetchval(_HEAD_SQL, job_card_id) or job_card_id
    cards = [dict(r) for r in await conn.fetch(_CHAIN_SQL, head)]
    return Scope(job_card_id=card["job_card_id"], job_card_number=card["job_card_number"],
                 plan_line_id=card["plan_line_id"], bom_id=card["bom_id"],
                 scope_job_card_id=int(head), cards=cards)


@dataclass
class Changes:
    removed: list[dict] = field(default_factory=list)
    added: list[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.removed and not self.added

    def by_key(self) -> dict[str, dict]:
        return {article_key(c["material_sku_name"]): c for c in [*self.removed, *self.added]}


async def load_changes(conn, scope_job_card_id: int) -> Changes:
    if not await table_exists(conn):
        return Changes()
    out = Changes()
    for r in await conn.fetch(_CHANGES_SQL, scope_job_card_id):
        row = dict(r)
        (out.removed if row["change_type"] == "removed" else out.added).append(row)
    return out


# ── the effective BOM ───────────────────────────────────────────────────────

def added_line(c: dict) -> dict:
    t = type_of(c["item_type"])
    return {"bom_line_id": None, "line_number": None, "material_sku_name": c["material_sku_name"],
            "item_type": t, "uom": UOM_FOR.get(t, "KGS"), "quantity_per_unit": None,
            "loss_pct": None, "godown": None, "added": True, "change_id": c["change_id"],
            "sku_id": c.get("sku_id"), "required_qty": _json(c.get("required_qty")),
            "required_unit": c.get("required_unit")}


def _indent_as_line(i: dict) -> dict:
    return {"bom_line_id": i.get("bom_line_id"), "line_number": None,
            "material_sku_name": i.get("material_sku_name"), "item_type": type_of(i.get("item_type")),
            "uom": i.get("uom"), "quantity_per_unit": None, "loss_pct": _json(i.get("loss_pct")),
            "godown": i.get("godown")}


def effective_bom_lines(master_lines: Iterable[dict], indent_lines: Iterable[dict],
                        changes: Changes) -> tuple[list[dict], dict[str, set[int]]]:
    """The card's BOM lines minus the removed articles plus the added ones.
    No changes -> the master lines unchanged (payload exactly as before)."""
    flags: dict[str, set[int]] = {"not_on_bom": set(), "superseded": set()}
    master = [dict(l) for l in master_lines]
    if changes.is_empty():
        return master, flags
    base = master or [_indent_as_line(dict(i)) for i in indent_lines]
    base_keys = {article_key(l.get("material_sku_name")) for l in base}
    removed_keys: set[str] = set()
    for c in changes.removed:
        k = article_key(c["material_sku_name"])
        removed_keys.add(k)
        if k not in base_keys:
            flags["not_on_bom"].add(c["change_id"])
    lines = [l for l in base if article_key(l.get("material_sku_name")) not in removed_keys]
    for c in changes.added:
        if article_key(c["material_sku_name"]) in base_keys:
            flags["superseded"].add(c["change_id"])
            continue
        lines.append(added_line(c))
    return lines, flags


def changes_payload(scope_job_card_id: int, changes: Changes, flags: dict[str, set[int]]) -> dict:
    def common(c: dict) -> dict:
        return {"change_id": c["change_id"], "material_sku_name": c["material_sku_name"],
                "item_type": type_of(c["item_type"]), "note": c.get("note"),
                "changed_by": c.get("changed_by"), "changed_at": _json(c.get("changed_at")),
                "made_on_job_card_number": c.get("made_on_job_card_number")}
    return {
        "scope_job_card_id": scope_job_card_id,
        "removed": [{**common(c), "not_on_bom": c["change_id"] in flags["not_on_bom"]}
                    for c in changes.removed],
        "added": [{**common(c), "sku_id": c.get("sku_id"), "required_qty": _json(c.get("required_qty")),
                   "required_unit": c.get("required_unit"),
                   "superseded": c["change_id"] in flags["superseded"]} for c in changes.added],
    }


async def detail_bom(conn, job_card_id: int, master_lines: list[dict],
                     rm_indents: list[dict], pm_indents: list[dict]) -> tuple[list[dict], Optional[dict]]:
    """get_job_card's bom_lines and bom_changes. Inputs are serialised dicts.
    Before migration 115 (or for a deleted card): the master lines, no block."""
    if not await table_exists(conn):
        return list(master_lines), None
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        return list(master_lines), None
    changes = await load_changes(conn, scope.scope_job_card_id)
    indents = [{**r, "item_type": RM} for r in rm_indents] + [{**r, "item_type": PM} for r in pm_indents]
    lines, flags = effective_bom_lines(master_lines, indents, changes)
    return lines, changes_payload(scope.scope_job_card_id, changes, flags)


# ── saved figures ───────────────────────────────────────────────────────────

_KIND_LABEL = {"consumption": "consumption", "returned": "returned to store",
               "extra_given": "extra giveaway", "wastage": "wastage",
               "control_sample": "control sample", "offgrade": "off-grade"}


async def has_records(conn, scope: Scope, key: str, bom_line_ids: list[int]) -> list[dict]:
    rows = await conn.fetch(_RECORDS_SQL, scope.card_ids, key, [int(i) for i in bom_line_ids])
    return [_row(r) for r in rows]


def _qty_text(q: Any) -> str:
    return f"{float(q):g}"


def records_message(article: str, hits: list[dict]) -> str:
    parts: list[str] = []
    for h in hits:
        card = h.get("job_card_number")
        if h["kind"] == "issued":
            part = f"{_qty_text(h.get('qty'))} was received on {card} (return it to store first)"
        else:
            where = (f"batch {h.get('batch_number')}" if h.get("batch_id") is not None
                     else "saved without a batch")
            part = f"{_KIND_LABEL.get(h['kind'], h['kind'])} is saved on {card}, {where}"
            if h.get("batch_id") is not None and h.get("batch_status") not in (None, "open"):
                part += f" ({h.get('batch_status')}: an admin must re-open it with the override)"
        if part not in parts:
            parts.append(part)
    return (f"{article} can't be removed yet: " + "; ".join(parts) + ". "
            "Clear the figures in Accounting (clearing a figure saves 0), then remove it.")


# ── locks ───────────────────────────────────────────────────────────────────

async def lock_line(conn, plan_line_id: int, *, exclusive: bool) -> None:
    """The plan line row is the lock for BOM changes. It MUST be the first row
    lock the transaction takes (lock-order rule, spec 2c)."""
    await conn.fetchval(_LOCK_UPDATE_SQL if exclusive else _LOCK_SHARE_SQL, plan_line_id)


async def lock_line_for_card(conn, job_card_id: int) -> Optional[int]:
    """KEY SHARE on the card's plan line: taken by the paths that check an
    article against the job card's BOM and then write (Save Output,
    requisitions, receive-material). Does not conflict with other saves or
    their non-key updates; waits for a BOM change on the same line."""
    return await conn.fetchval(_LOCK_CARD_SHARE_SQL, job_card_id)
```

Note: the `records_message` expected output in the test pins the exact wording and qty formatting (`12` for `12.0` via `:g`).

- [ ] **Step 4: Run — expect PASS.** `.venv/Scripts/python -m pytest tests/services/test_jc_bom_changes.py -q`

---

### Task S3: `jc_bom_changes` — writes, and the two endpoints

**Files:**
- Modify: `server_replica/app/modules/production/services/jc_bom_changes.py` (append)
- Modify: `server_replica/app/modules/production/router.py` (new routes near the other `/job-cards-v2/{job_card_id}/…` routes; keep CRLF)
- Test: `server_replica/tests/services/test_jc_bom_changes_writes.py` (new)

**Interfaces:**
- Consumes: S2 names.
- Produces: `async remove_article(conn, *, actor, job_card_id, material_sku_name, note=None) -> dict`; `async add_article(conn, *, actor, job_card_id, sku_id, required_qty=None, note=None) -> dict`; `async undo_change(conn, *, actor, job_card_id, change_id) -> dict`; `parse_required(value, item_type) -> tuple[Decimal | None, str | None]`; `async open_requisitions(conn, scope, key) -> list[int]`. Result dict: `{"action": "removed"|"add_undone"|"added"|"restored", "restored": bool, "open_requisition_ids": [int], "bom_changes": dict, "bom_lines": [dict]}`.
- Routes: `POST /api/v1/production/job-cards-v2/{job_card_id}/bom-changes` body `{action: "remove"|"add", material_sku_name?, sku_id?, required_qty?, note?}`; `DELETE /api/v1/production/job-cards-v2/{job_card_id}/bom-changes/{change_id}`. Both `require_permission("production", "job_cards", "overview", action="start")`. Errors: HTTP `e.http_status`, `detail = e.detail()`.

- [ ] **Step 1: Write the failing tests** — `tests/services/test_jc_bom_changes_writes.py` (reuse the tag-dispatch FakeConn by copying it; make `execute` for `jcbc:undo` return `"UPDATE 1"` by default):

```python
"""jc_bom_changes writes + the bom-changes routes (spec 2c)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import jc_bom_changes as m

TAG = re.compile(r"/\* (jcbc:[a-z_]+) \*/")
WHEN = datetime(2026, 9, 21, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, **answers):
        self.answers = {"jcbc:table": True, "jcbc:lock_update": 70, "jcbc:head": 1,
                        "jcbc:undo": "UPDATE 1", "jcbc:insert": 999, "jcbc:req_table": True,
                        "jcbc:open_reqs": [], "jcbc:records": [], "jcbc:indents": []}
        self.answers.update({k.replace("__", ":"): v for k, v in answers.items()})
        self.calls: list[tuple[str, tuple]] = []

    def _answer(self, sql, args):
        tag = TAG.search(sql).group(1)
        self.calls.append((tag, args))
        assert tag in self.answers, f"unexpected query {tag}"
        v = self.answers[tag]
        return v(*args) if callable(v) else v

    fetch = fetchrow = fetchval = execute = lambda self, sql, *a: _aw(self._answer(sql, a))

    def transaction(self):
        return _NullCtx()

    def tags(self):
        return [t for t, _ in self.calls]


async def _aw(v):
    return v


class _NullCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


CARD = {"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "plan_line_id": 70, "bom_id": 9, "status": "locked"}
CHAIN = [{"job_card_id": 1, "job_card_number": "PLAN-7-L1-S1", "status": "unlocked"},
         {"job_card_id": 3, "job_card_number": "PLAN-7-L1-S3", "status": "locked"}]
MASTER = [{"bom_line_id": 101, "line_number": 1, "material_sku_name": "Seeds", "item_type": "rm", "uom": "KGS",
           "quantity_per_unit": Decimal("0.5"), "loss_pct": Decimal("1"), "godown": None},
          {"bom_line_id": 102, "line_number": 2, "material_sku_name": "Pouch", "item_type": "pm", "uom": "NOS",
           "quantity_per_unit": Decimal("1"), "loss_pct": None, "godown": None},
          {"bom_line_id": 103, "line_number": 3, "material_sku_name": "SFG0001", "item_type": "sfg", "uom": "KGS",
           "quantity_per_unit": Decimal("1"), "loss_pct": None, "godown": None}]


def conn_with(changes=(), **over):
    return FakeConn(jcbc__card=CARD, jcbc__chain=CHAIN, jcbc__master=MASTER,
                    jcbc__changes=list(changes), **over)


def change(cid, ctype, name, itype="rm", **over):
    row = {"change_id": cid, "change_type": ctype, "material_sku_name": name, "item_type": itype,
           "sku_id": 55 if ctype == "added" else None, "required_qty": None, "required_unit": None,
           "note": None, "made_on_job_card_id": 1, "made_on_job_card_number": "PLAN-7-L1-S1",
           "changed_by": "Pat", "changed_at": WHEN}
    row.update(over)
    return row


def run(coro):
    return asyncio.run(coro)


def refused(coro) -> m.BomChangeError:
    with pytest.raises(m.BomChangeError) as e:
        run(coro)
    return e.value


# ── remove ──
def test_remove_a_bom_line_inserts_a_removed_row_scoped_to_the_chains_first_card():
    conn = conn_with()
    out = run(m.remove_article(conn, actor="Pat", job_card_id=3, material_sku_name=" seeds ", note=" not used "))
    ins = [a for t, a in conn.calls if t == "jcbc:insert"][0]
    # change_id, scope, plan_line, type, name, item_type, sku_id, qty, unit, note, made_on id, made_on number, actor
    assert ins[1:] == (1, 70, "removed", "Seeds", "rm", None, None, None, "not used", 3, "PLAN-7-L1-S3", "Pat")
    assert out["action"] == "removed" and out["restored"] is False
    # The plan line is locked before anything else is read about the chain.
    tags = conn.tags()
    assert tags.index("jcbc:lock_update") < tags.index("jcbc:head")


def test_remove_refuses_an_article_not_on_the_list_and_sfg_lines():
    assert refused(m.remove_article(conn_with(), actor="P", job_card_id=3, material_sku_name="Salt")).code \
        == "article_not_on_job_card"
    e = refused(m.remove_article(conn_with(), actor="P", job_card_id=3, material_sku_name="SFG0001"))
    assert (e.http_status, e.code) == (422, "not_rm_or_pm")


def test_remove_refuses_while_figures_are_saved():
    hits = [{"kind": "consumption", "qty": Decimal("3"), "job_card_id": 1, "job_card_number": "PLAN-7-L1-S1",
             "batch_id": 4, "batch_number": 1, "batch_status": "open"}]
    conn = conn_with(jcbc__records=hits)
    e = refused(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (409, "article_has_records")
    assert e.details["hits"][0]["job_card_number"] == "PLAN-7-L1-S1"
    assert ("jcbc:records", ([1, 3], "SEEDS", [101])) in conn.calls
    assert "jcbc:insert" not in conn.tags()


def test_remove_an_added_article_undoes_its_add():
    conn = conn_with([change(11, "added", "Salt")])
    out = run(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="salt"))
    assert ("jcbc:undo", (11, "P")) in conn.calls and "jcbc:insert" not in conn.tags()
    assert out["action"] == "add_undone"


def test_remove_a_superseded_add_undoes_it_then_removes_the_bom_line():
    conn = conn_with([change(11, "added", "seeds")])
    run(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Seeds"))
    tags = conn.tags()
    assert tags.index("jcbc:undo") < tags.index("jcbc:insert")


def test_remove_reports_open_requests_and_leaves_them():
    conn = conn_with(jcbc__open_reqs=[{"requisition_id": 27385955}])
    out = run(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Pouch"))
    assert out["open_requisition_ids"] == [27385955]
    assert ("jcbc:open_reqs", ([1, 3], "POUCH")) in conn.calls


def test_nothing_changes_once_the_job_card_is_finished():
    chain = [dict(c, status="completed") for c in CHAIN]
    e = refused(m.remove_article(conn_with(jcbc__chain=chain), actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (409, "job_card_finished")


def test_a_stage_waiting_for_the_previous_one_is_not_refused():
    # CARD.status is 'locked' (awaiting_previous_stage): the change goes through.
    run(m.remove_article(conn_with(), actor="P", job_card_id=3, material_sku_name="Seeds"))


def test_before_migration_115_writes_answer_503():
    e = refused(m.remove_article(conn_with(jcbc__table=False), actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (503, "bom_changes_not_available")


def test_missing_card_is_404():
    e = refused(m.remove_article(conn_with(jcbc__card=None), actor="P", job_card_id=3, material_sku_name="Seeds"))
    assert (e.http_status, e.code) == (404, "job_card_not_found")


# ── add ──
SKU_SALT = {"sku_id": 55, "particulars": "  Salt ", "item_type": "RM"}


def test_add_inserts_an_added_row_with_the_required_qty():
    conn = conn_with(jcbc__sku=SKU_SALT)
    out = run(m.add_article(conn, actor="P", job_card_id=3, sku_id=55, required_qty="2.5"))
    ins = [a for t, a in conn.calls if t == "jcbc:insert"][0]
    assert ins[3:10] == ("added", "Salt", "rm", 55, Decimal("2.5"), "kg", None)
    assert out["action"] == "added"


def test_add_refuses_unknown_skus_and_other_types():
    assert refused(m.add_article(conn_with(jcbc__sku=None), actor="P", job_card_id=3, sku_id=1)).code == "sku_not_found"
    fg = {"sku_id": 2, "particulars": "Healthy Choice 100 g", "item_type": "fg"}
    assert refused(m.add_article(conn_with(jcbc__sku=fg), actor="P", job_card_id=3, sku_id=2)).code == "not_rm_or_pm"


def test_add_refuses_an_article_already_on_the_list_even_with_odd_spaces():
    dup = {"sku_id": 9, "particulars": "Seeds", "item_type": "rm"}
    e = refused(m.add_article(conn_with(jcbc__sku=dup), actor="P", job_card_id=3, sku_id=9))
    assert (e.code, e.details["bom_spelling"]) == ("already_on_job_card", "Seeds")
    master = [dict(MASTER[0], material_sku_name="Roasted  Seeds")]
    nbsp = {"sku_id": 9, "particulars": "Roasted Seeds", "item_type": "rm"}
    e = refused(m.add_article(conn_with(jcbc__sku=nbsp, jcbc__master=master), actor="P", job_card_id=3, sku_id=9))
    assert e.details["bom_spelling"] == "Roasted  Seeds"


def test_adding_a_removed_bom_article_restores_it():
    conn = conn_with([change(10, "removed", "Seeds")], jcbc__sku={"sku_id": 9, "particulars": "Seeds", "item_type": "rm"})
    out = run(m.add_article(conn, actor="P", job_card_id=3, sku_id=9, required_qty="4"))
    assert ("jcbc:undo", (10, "P")) in conn.calls and "jcbc:insert" not in conn.tags()
    assert out["restored"] is True and out["action"] == "restored"


@pytest.mark.parametrize("value,itype,code_or_result", [
    (None, "rm", (None, None)), ("", "pm", (None, None)), ("2.500", "rm", (Decimal("2.500"), "kg")),
    ("100", "pm", (Decimal("100"), "pcs")), ("0", "rm", "required_qty_invalid"),
    ("-1", "rm", "required_qty_invalid"), ("1.5", "pm", "required_qty_invalid"),
    ("1.2345", "rm", "required_qty_invalid"), ("abc", "rm", "required_qty_invalid"),
    (True, "rm", "required_qty_invalid"), ("1e12", "rm", "required_qty_invalid"),
])
def test_parse_required(value, itype, code_or_result):
    if isinstance(code_or_result, str):
        with pytest.raises(m.BomChangeError) as e:
            m.parse_required(value, itype)
        assert e.value.code == code_or_result
    else:
        assert m.parse_required(value, itype) == code_or_result


# ── undo ──
def test_undo_restores_a_removed_article():
    conn = conn_with([change(10, "removed", "Seeds")],
                     jcbc__change={"change_id": 10, "change_type": "removed", "material_sku_name": "Seeds",
                                   "undone_at": None})
    out = run(m.undo_change(conn, actor="P", job_card_id=3, change_id=10))
    assert ("jcbc:change", (10, 1)) in conn.calls and out["restored"] is True


def test_undo_of_an_added_article_checks_records():
    hits = [{"kind": "offgrade", "qty": Decimal("1"), "job_card_id": 3, "job_card_number": "S3",
             "batch_id": 7, "batch_number": 1, "batch_status": "open"}]
    conn = conn_with([change(11, "added", "Salt")], jcbc__records=hits,
                     jcbc__change={"change_id": 11, "change_type": "added", "material_sku_name": "Salt",
                                   "undone_at": None})
    assert refused(m.undo_change(conn, actor="P", job_card_id=3, change_id=11)).code == "article_has_records"


def test_undo_refusals():
    assert refused(m.undo_change(conn_with(jcbc__change=None), actor="P", job_card_id=3, change_id=5)).code \
        == "change_not_found"
    done = {"change_id": 5, "change_type": "removed", "material_sku_name": "Seeds", "undone_at": WHEN}
    assert refused(m.undo_change(conn_with(jcbc__change=done), actor="P", job_card_id=3, change_id=5)).code \
        == "change_already_undone"


def test_a_lost_race_on_the_live_index_is_409():
    class Clash(Exception):
        constraint_name = m.LIVE_INDEX

    import asyncpg

    def boom(*a):
        raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint "
                                           f'"{m.LIVE_INDEX}"')
    conn = conn_with(jcbc__insert=boom)
    assert refused(m.remove_article(conn, actor="P", job_card_id=3, material_sku_name="Seeds")).code \
        == "bom_changed_concurrently"


# ── routes ──
class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *exc):
        return False


def _request(conn):
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))


USER = SimpleNamespace(full_name="Planner Pat", email=None, phone=None, user_id=7)


def test_routes_exist_with_the_job_card_edit_permission():
    paths = {(r.path, tuple(sorted(r.methods))) for r in PR.router.routes if hasattr(r, "methods")}
    assert any(p.endswith("/job-cards-v2/{job_card_id}/bom-changes") and "POST" in ms for p, ms in paths)
    assert any(p.endswith("/job-cards-v2/{job_card_id}/bom-changes/{change_id}") and "DELETE" in ms
               for p, ms in paths)


def test_route_maps_a_refusal_to_its_status(monkeypatch):
    async def refuse(conn, **kw):
        raise m.BomChangeError(409, "article_has_records", "Seeds can't be removed yet", hits=[])
    monkeypatch.setattr(m, "remove_article", refuse)
    conn = FakeConn()
    with pytest.raises(HTTPException) as e:
        run(PR.create_bom_change(_request(conn), 3, PR.BomChangeBody(action="remove", material_sku_name="Seeds"),
                                 user=USER))
    assert e.value.status_code == 409
    assert e.value.detail == {"error": "article_has_records", "message": "Seeds can't be removed yet", "hits": []}


def test_route_add_passes_the_fields(monkeypatch):
    seen = {}

    async def add(conn, **kw):
        seen.update(kw)
        return {"action": "added"}
    monkeypatch.setattr(m, "add_article", add)
    out = run(PR.create_bom_change(_request(FakeConn()), 3,
                                   PR.BomChangeBody(action="add", sku_id=55, required_qty="2.5", note="x"), user=USER))
    assert out == {"action": "added"}
    assert seen == {"actor": "Planner Pat", "job_card_id": 3, "sku_id": 55, "required_qty": "2.5", "note": "x"}


def test_route_add_needs_a_sku_and_remove_needs_a_name():
    with pytest.raises(HTTPException) as e:
        run(PR.create_bom_change(_request(FakeConn()), 3, PR.BomChangeBody(action="add"), user=USER))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run(PR.create_bom_change(_request(FakeConn()), 3, PR.BomChangeBody(action="remove"), user=USER))
    assert e.value.status_code == 422
```

Adjust `USER` to whatever `_actor_name(user)` reads (it uses `full_name`, then email, phone, id). If `PR.router` exposes routes with a prefix, the `endswith` checks still pass.

- [ ] **Step 2: Run — expect FAIL** (functions missing).

- [ ] **Step 3: Append to `jc_bom_changes.py`:**

```python
# ── writes (spec 2c) ────────────────────────────────────────────────────────

_SKU_SQL = "/* jcbc:sku */ SELECT sku_id, particulars, item_type FROM all_sku WHERE sku_id = $1"

_INSERT_SQL = """/* jcbc:insert */
    INSERT INTO job_card_bom_change (
        change_id, scope_job_card_id, plan_line_id, change_type, material_sku_name, item_type,
        sku_id, required_qty, required_unit, note, made_on_job_card_id, made_on_job_card_number,
        changed_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    RETURNING change_id
"""

_UNDO_SQL = """/* jcbc:undo */
    UPDATE job_card_bom_change SET undone_at = now(), undone_by = $2
     WHERE change_id = $1 AND undone_at IS NULL
"""

_CHANGE_SQL = """/* jcbc:change */
    SELECT change_id, change_type, material_sku_name, undone_at
      FROM job_card_bom_change
     WHERE change_id = $1 AND scope_job_card_id = $2
"""

_REQ_TABLE_SQL = "/* jcbc:req_table */ SELECT to_regclass('public.floor_requisition') IS NOT NULL"

_OPEN_REQS_SQL = """/* jcbc:open_reqs */
    SELECT requisition_id FROM floor_requisition
     WHERE job_card_id = ANY($1::bigint[]) AND UPPER(BTRIM(material_sku_name)) = $2
       AND status = 'raised'
     ORDER BY requisition_id
"""


@dataclass
class _State:
    scope: Scope
    changes: Changes
    master: list[dict]
    lines: list[dict]
    flags: dict[str, set[int]]


async def _state(conn, job_card_id: int) -> _State:
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        raise BomChangeError(404, "job_card_not_found", f"Job card {job_card_id} does not exist.")
    changes = await load_changes(conn, scope.scope_job_card_id)
    master = ([_row(r) for r in await conn.fetch(_MASTER_SQL, scope.bom_id)]
              if scope.bom_id is not None else [])
    indents = [] if master else [_row(r) for r in await conn.fetch(_INDENTS_SQL, job_card_id)]
    lines, flags = effective_bom_lines(master, indents, changes)
    return _State(scope, changes, master, lines, flags)


async def _open(conn, job_card_id: int) -> _State:
    if not await table_exists(conn):
        raise BomChangeError(503, "bom_changes_not_available",
                             "BOM changes need database migration 115, which has not been applied yet.")
    card = await conn.fetchrow(_CARD_SQL, job_card_id)
    if card is None:
        raise BomChangeError(404, "job_card_not_found", f"Job card {job_card_id} does not exist.")
    # Lock first, then re-read the chain (lock-order rule, spec 2c).
    await lock_line(conn, card["plan_line_id"], exclusive=True)
    st = await _state(conn, job_card_id)
    if st.scope.finished:
        raise BomChangeError(409, "job_card_finished",
                             "Every stage of this job card is completed, closed or cancelled, "
                             "so its BOM can no longer be changed.")
    return st


async def _result(conn, job_card_id: int, action: str, *, restored: bool = False,
                  open_requisition_ids: Optional[list[int]] = None) -> dict:
    st = await _state(conn, job_card_id)
    return {"action": action, "restored": restored,
            "open_requisition_ids": open_requisition_ids or [],
            "bom_changes": changes_payload(st.scope.scope_job_card_id, st.changes, st.flags),
            "bom_lines": st.lines}


async def _insert(conn, scope: Scope, *, actor: str, change_type: str, name: str, item_type: str,
                  sku_id: Optional[int] = None, required_qty: Optional[Decimal] = None,
                  required_unit: Optional[str] = None, note: Optional[str] = None) -> int:
    async def _do():
        return await conn.fetchval(
            _INSERT_SQL, new_short_time_id(), scope.scope_job_card_id, scope.plan_line_id,
            change_type, name, item_type, sku_id, required_qty, required_unit, _clean(note),
            scope.job_card_id, scope.job_card_number, actor)
    try:
        return await insert_with_pk_retry(conn, _do)
    except asyncpg.UniqueViolationError as exc:
        if LIVE_INDEX in ((getattr(exc, "constraint_name", None) or "") + str(exc)):
            raise BomChangeError(409, "bom_changed_concurrently",
                                 "Someone changed this article on this job card just now. "
                                 "Reload and try again.") from None
        raise


async def _undo(conn, change_id: int, actor: str) -> None:
    status = await conn.execute(_UNDO_SQL, change_id, actor)
    if not str(status).endswith(" 1"):
        raise BomChangeError(409, "change_already_undone", "That change was already undone. Reload.")


async def _refuse_if_records(conn, scope: Scope, article: str, key: str, bom_line_ids: list[int]) -> None:
    hits = await has_records(conn, scope, key, bom_line_ids)
    if hits:
        raise BomChangeError(409, "article_has_records", records_message(article, hits), hits=hits)


async def open_requisitions(conn, scope: Scope, key: str) -> list[int]:
    if not await conn.fetchval(_REQ_TABLE_SQL):
        return []
    return [r["requisition_id"] for r in await conn.fetch(_OPEN_REQS_SQL, scope.card_ids, key)]


def parse_required(value: Any, item_type: str) -> tuple[Optional[Decimal], Optional[str]]:
    """An optional required qty: > 0, at most 3 decimals, whole for PM."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    def bad(msg: str) -> BomChangeError:
        return BomChangeError(422, "required_qty_invalid", msg)
    if isinstance(value, bool):
        raise bad("Enter the required quantity as a number.")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise bad("Enter the required quantity as a number.") from None
    if not d.is_finite() or d <= 0:
        raise bad("The required quantity must be more than 0.")
    if d >= Decimal("1e11"):
        raise bad("That quantity is too large.")
    exp = d.normalize().as_tuple().exponent
    decimals = -exp if isinstance(exp, int) and exp < 0 else 0
    unit = UNIT_FOR[item_type]
    if unit == "pcs" and decimals > 0:
        raise bad("Pieces are whole numbers.")
    if decimals > 3:
        raise bad("Kilograms go to 3 decimals at most.")
    return d, unit


async def remove_article(conn, *, actor: str, job_card_id: int, material_sku_name: Optional[str],
                         note: Optional[str] = None) -> dict:
    st = await _open(conn, job_card_id)
    key = article_key(material_sku_name)
    on_list = [l for l in st.lines if key and article_key(l.get("material_sku_name")) == key]
    if not on_list:
        raise BomChangeError(422, "article_not_on_job_card",
                             f"{(material_sku_name or '').strip()!r} is not on this job card's BOM.")
    line = on_list[0]
    item_type = type_of(line.get("item_type"))
    if item_type not in ITEM_TYPES:
        raise BomChangeError(422, "not_rm_or_pm",
                             f"{line['material_sku_name']} is {item_type.upper() or 'untyped'}; "
                             "only RM and PM articles can be removed.")
    bom_ids = [int(l["bom_line_id"]) for l in st.master
               if article_key(l.get("material_sku_name")) == key and l.get("bom_line_id") is not None]
    await _refuse_if_records(conn, st.scope, line["material_sku_name"], key, bom_ids)
    if line.get("added"):
        await _undo(conn, line["change_id"], actor)
        action = "add_undone"
    else:
        superseded = st.changes.by_key().get(key)
        if superseded is not None:          # an added row the BOM has since caught up with
            await _undo(conn, superseded["change_id"], actor)
        await _insert(conn, st.scope, actor=actor, change_type="removed",
                      name=line["material_sku_name"], item_type=item_type, note=note)
        action = "removed"
    return await _result(conn, job_card_id, action,
                         open_requisition_ids=await open_requisitions(conn, st.scope, key))


async def add_article(conn, *, actor: str, job_card_id: int, sku_id: int,
                      required_qty: Any = None, note: Optional[str] = None) -> dict:
    st = await _open(conn, job_card_id)
    sku = await conn.fetchrow(_SKU_SQL, sku_id)
    if sku is None:
        raise BomChangeError(404, "sku_not_found", f"SKU {sku_id} is not in the SKU master.")
    name = (sku["particulars"] or "").strip()
    item_type = type_of(sku["item_type"])
    if item_type not in ITEM_TYPES:
        raise BomChangeError(422, "not_rm_or_pm",
                             f"{name} is {item_type.upper() or 'untyped'}; only RM and PM articles can be added.")
    key = article_key(name)
    live = st.changes.by_key().get(key)
    if live is not None and live["change_type"] == "removed":
        await _undo(conn, live["change_id"], actor)
        return await _result(conn, job_card_id, "restored", restored=True)
    for l in st.lines:
        spelled = l.get("material_sku_name")
        if article_key(spelled) == key or loose_key(spelled) == loose_key(name):
            raise BomChangeError(409, "already_on_job_card",
                                 f"{spelled} is already on this job card's BOM.", bom_spelling=spelled)
    qty, unit = parse_required(required_qty, item_type)
    await _insert(conn, st.scope, actor=actor, change_type="added", name=name, item_type=item_type,
                  sku_id=int(sku["sku_id"]), required_qty=qty, required_unit=unit, note=note)
    return await _result(conn, job_card_id, "added")


async def undo_change(conn, *, actor: str, job_card_id: int, change_id: int) -> dict:
    st = await _open(conn, job_card_id)
    row = await conn.fetchrow(_CHANGE_SQL, change_id, st.scope.scope_job_card_id)
    if row is None:
        raise BomChangeError(404, "change_not_found", f"Change {change_id} is not on this job card.")
    if row["undone_at"] is not None:
        raise BomChangeError(409, "change_already_undone", "That change was already undone. Reload.")
    if row["change_type"] == "added" and row["change_id"] not in st.flags["superseded"]:
        await _refuse_if_records(conn, st.scope, row["material_sku_name"],
                                 article_key(row["material_sku_name"]), [])
    await _undo(conn, change_id, actor)
    restored = row["change_type"] == "removed"
    return await _result(conn, job_card_id, "restored" if restored else "add_undone", restored=restored)
```

The fake conn's `jcbc:insert` answer is `999` (a change id); the lost-race test replaces it with a function that raises `asyncpg.UniqueViolationError`. Because `insert_with_pk_retry` wraps the call in `conn.transaction()` (a SAVEPOINT), the FakeConn provides `transaction()`; check `app/core/helpers.py` and adapt the fake if the helper inspects the exception differently (it must re-raise non-PK unique violations).

- [ ] **Step 4: Add the routes to `app/modules/production/router.py`** (CRLF), near the other job-card-v2 routes (e.g. after `create_box_scan`):

```python
class BomChangeBody(BaseModel):
    """POST /job-cards-v2/{id}/bom-changes — remove an article from, or add one to,
    this job card's BOM (every stage of its chain). The BOM module is not written."""
    action: Literal["remove", "add"]
    material_sku_name: str | None = Field(default=None, max_length=300)
    sku_id: int | None = None
    required_qty: str | float | None = None
    note: str | None = Field(default=None, max_length=500)


@router.post("/job-cards-v2/{job_card_id}/bom-changes")
async def create_bom_change(
    request: Request, job_card_id: int, body: BomChangeBody,
    user=Depends(require_permission("production", "job_cards", "overview", action="start")),
):
    from app.modules.production.services import jc_bom_changes as bomc
    if body.action == "add" and body.sku_id is None:
        raise HTTPException(status_code=422, detail={"error": "sku_required", "message": "Pick an article to add."})
    if body.action == "remove" and not (body.material_sku_name or "").strip():
        raise HTTPException(status_code=422, detail={"error": "article_required",
                                                     "message": "Name the article to remove."})
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                if body.action == "remove":
                    return await bomc.remove_article(conn, actor=_actor_name(user), job_card_id=job_card_id,
                                                     material_sku_name=body.material_sku_name, note=body.note)
                return await bomc.add_article(conn, actor=_actor_name(user), job_card_id=job_card_id,
                                              sku_id=body.sku_id, required_qty=body.required_qty, note=body.note)
            except bomc.BomChangeError as e:
                raise HTTPException(status_code=e.http_status, detail=e.detail()) from None


@router.delete("/job-cards-v2/{job_card_id}/bom-changes/{change_id}")
async def undo_bom_change(
    request: Request, job_card_id: int, change_id: int,
    user=Depends(require_permission("production", "job_cards", "overview", action="start")),
):
    from app.modules.production.services import jc_bom_changes as bomc
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                return await bomc.undo_change(conn, actor=_actor_name(user), job_card_id=job_card_id,
                                              change_id=change_id)
            except bomc.BomChangeError as e:
                raise HTTPException(status_code=e.http_status, detail=e.detail()) from None
```

Check that `Literal` and `Field` are already imported in router.py (they are used elsewhere; add to the imports if not). The test route helper passes `required_qty="2.5"`; the route forwards it unchanged. In the route test, `_actor_name(USER)` must yield "Planner Pat" — adjust `USER`'s attributes to what `_actor_name` reads.

- [ ] **Step 5: Run — expect PASS.** `.venv/Scripts/python -m pytest tests/services/test_jc_bom_changes.py tests/services/test_jc_bom_changes_writes.py -q`
- [ ] **Step 6: Line endings** of router.py unchanged (LF-only 0).

---

### Task S4: `get_job_card` returns the effective list and `bom_changes`

**Files:**
- Modify: `server_replica/app/modules/production/services/job_card_v2.py` (`get_job_card`, return dict ~line 5369)
- Test: add to `server_replica/tests/services/test_jc_bom_changes.py`

**Interfaces:** Consumes `detail_bom`. Produces detail keys `bom_lines` (effective) and `bom_changes` (dict or `None` before 115).

- [ ] **Step 1: Failing test** — append to `test_jc_bom_changes.py`:

```python
def test_get_job_card_uses_detail_bom(monkeypatch):
    """get_job_card hands its bom_line rows and indents to detail_bom and returns
    what comes back as bom_lines + bom_changes."""
    import inspect
    from app.modules.production.services import job_card_v2 as jcv2
    src = inspect.getsource(jcv2.get_job_card)
    assert "jc_bom_changes" in src and "detail_bom(" in src
    assert re.search(r'"bom_changes":\s+bom_changes_out', src)
    assert re.search(r'"bom_lines":\s+bom_lines_out', src)
```

(`get_job_card` performs ~25 queries; a behavioural test would need a very large fake. The behaviour lives in `detail_bom`, tested in S2; this test pins the wiring.)

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Edit `get_job_card`** (CRLF). After the `bom_line_rows` fetch block (~line 4980) and after `section_2a_rm_indent` / `section_2b_pm_indent` are built (just before `return {`), add:

```python
    # Per-job-card BOM changes (migration 115): the chain's removed articles
    # drop out and its added ones are appended. Before 115, or with no changes,
    # this is exactly the bom_line catalogue.
    from app.modules.production.services import jc_bom_changes
    bom_lines_out, bom_changes_out = await jc_bom_changes.detail_bom(
        conn, job_card_id, [_serialize(r) for r in bom_line_rows],
        section_2a_rm_indent, section_2b_pm_indent,
    )
```

and in the returned dict replace `"bom_lines":         [_serialize(r) for r in bom_line_rows],` with:

```python
        "bom_lines":         bom_lines_out,
        "bom_changes":       bom_changes_out,
```

Search `get_job_card` for any other use of `bom_line_rows` after this point and leave it unchanged (only the payload changes).

- [ ] **Step 4: Run — PASS**, plus `tests/services -q -k "job_card"` to catch fakes of `get_job_card` that now see extra queries (`jcbc:table`). If an existing test fakes `get_job_card`'s connection, teach its fake to answer `SELECT to_regclass('public.job_card_bom_change')` with `False`.

---

### Task S5: Save Output — lock, article checks, id coercion

**Files:**
- Modify: `server_replica/app/modules/production/services/jc_bom_changes.py` (append `ArticleResolver`, `resolver_for`)
- Modify: `server_replica/app/modules/production/router.py` (`ConsumedLineV2` ~6356, `ByproductLineV2` ~4232, `BalanceMaterialV2` ~4254, `record_output_v2` ~6494-6722)
- Test: `server_replica/tests/services/test_save_output_bom_changes.py` (new)

**Interfaces:**
- Produces: `class ArticleResolver` with `build(master_lines, changes)`, `state(name) -> "bom"|"added"|"removed"|"none"`, `is_removed(*, name=None, bom_line_id=None) -> bool`, `canonical_name(name) -> str`, `added_row(name) -> dict|None`, `item_type(name) -> str|None`, `check_consumption(lines) -> dict|None`, `check_rows(*, balance, byproducts) -> dict|None`, `stored_entry(entry) -> dict`; `async resolver_for(conn, job_card_id) -> ArticleResolver`; `_coerce_line_id(v) -> int | None` in router.

- [ ] **Step 1: Failing tests** — `tests/services/test_save_output_bom_changes.py`:

```python
"""Save Output honours the job card's BOM changes (spec 2d)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as PR
from app.modules.production.services import jc_bom_changes as m

MASTER = [{"bom_line_id": 101, "material_sku_name": "Seeds", "item_type": "rm"},
          {"bom_line_id": 102, "material_sku_name": "Pouch", "item_type": "pm"}]


def resolver(removed=(), added=()):
    ch = m.Changes(removed=[{"change_id": 1, "change_type": "removed", "material_sku_name": n, "item_type": "rm"}
                            for n in removed],
                   added=[{"change_id": 2, "change_type": "added", "material_sku_name": n, "item_type": t,
                           "required_qty": None} for n, t in added])
    return m.ArticleResolver.build(MASTER, ch)


# ── the resolver ──
def test_states():
    r = resolver(removed=["Pouch"], added=[("Salt", "rm")])
    assert (r.state("seeds"), r.state("POUCH"), r.state(" salt "), r.state("Sugar"), r.state("")) == \
        ("bom", "removed", "added", "none", "none")
    assert r.is_removed(bom_line_id=102) and r.is_removed(name="pouch") and not r.is_removed(bom_line_id=101)
    assert r.canonical_name("SALT") == "Salt" and r.item_type("salt") == "rm" and r.item_type("POUCH") == "pm"


def test_a_superseded_add_counts_as_the_bom_line():
    r = resolver(added=[("seeds", "rm")])
    assert r.state("Seeds") == "bom" and r.added_row("Seeds") is None


def test_check_consumption():
    r = resolver(removed=["Pouch"], added=[("Salt", "rm")])
    assert r.check_consumption([{"bom_line_id": 101, "material_sku_name": "Seeds"},
                                {"bom_line_id": None, "material_sku_name": "salt"}]) is None
    assert r.check_consumption([{"bom_line_id": 999, "material_sku_name": "X"}])["error"] == "invalid_bom_line"
    assert r.check_consumption([{"bom_line_id": 102, "material_sku_name": "Pouch"}])["error"] \
        == "article_removed_from_job_card"
    assert r.check_consumption([{"bom_line_id": None, "material_sku_name": "pouch"}])["error"] \
        == "article_removed_from_job_card"
    assert r.check_consumption([{"bom_line_id": None, "material_sku_name": "Sugar"}])["error"] \
        == "article_not_on_job_card"


def test_check_rows_only_cares_about_quantities_above_zero():
    r = resolver(removed=["Pouch"])
    assert r.check_rows(balance=[{"material_name": "Pouch", "qty_kg": 0, "bom_line_id": 102}], byproducts=[]) is None
    assert r.check_rows(balance=[{"material_name": "CONSOLIDATED", "qty_kg": 3}], byproducts=[]) is None
    assert r.check_rows(balance=[{"material_name": "Pouch", "qty_kg": 2}], byproducts=[])["error"] \
        == "article_removed_from_job_card"
    assert r.check_rows(balance=[], byproducts=[{"material_name": "pouch", "qty_kg": 1}])["error"] \
        == "article_removed_from_job_card"


def test_stored_entry_uses_the_canonical_spelling_for_lines_without_an_id():
    r = resolver(added=[("Salt", "rm")])
    assert r.stored_entry({"bom_line_id": None, "material_sku_name": " SALT "})["material_sku_name"] == "Salt"
    e = {"bom_line_id": 101, "material_sku_name": "seeds"}
    assert r.stored_entry(e) is e


# ── id coercion on the /outputs models ──
@pytest.mark.parametrize("v,want", [(0, None), (-3, None), ("", None), (None, None), (101, 101), ("101", 101)])
def test_line_ids_at_or_below_zero_are_none(v, want):
    assert PR.ConsumedLineV2(bom_line_id=v, material_sku_name="X", consumed_qty=1).bom_line_id == want
    assert PR.BalanceMaterialV2(bom_line_id=v, material_name="X", balance_type="returned", qty_kg=0).bom_line_id == want
    assert PR.ByproductLineV2(bom_line_id=v, category="offgrade", qty_kg=1).bom_line_id == want


def test_consumed_line_id_is_optional():
    assert PR.ConsumedLineV2(material_sku_name="Salt", consumed_qty=0).bom_line_id is None


# ── the route ──
class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *exc):
        return False


class Conn:
    def __init__(self):
        self.order: list[str] = []

    def transaction(self):
        return _Ctx(self)

    async def fetchrow(self, sql, *args):
        self.order.append("batch")
        return {"status": "open", "job_card_id": 3}

    async def fetch(self, sql, *args):
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        raise AssertionError(sql)


def _patch(monkeypatch, conn, res):
    from app.modules.production.services import job_card_v2 as jcv2
    written: list[tuple[str, list[dict]]] = []

    async def not_locked(c, jc):
        return None

    async def lock(c, jc):
        conn.order.append("lock")
        return 70

    async def get_resolver(c, jc):
        conn.order.append("resolver")
        return res

    async def upsert(c, *, job_card_id, entries, input_kind, recorded_by, batch_id=None):
        written.append((input_kind, entries))
        return len(entries)
    monkeypatch.setattr(jcv2, "assert_not_locked", not_locked)
    monkeypatch.setattr(jcv2, "upsert_consumption_lines", upsert)
    monkeypatch.setattr(m, "lock_line_for_card", lock)
    monkeypatch.setattr(m, "resolver_for", get_resolver)
    return written


def _call(conn, body):
    pool = SimpleNamespace(acquire=lambda: _Ctx(conn))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))
    user = SimpleNamespace(full_name="Op", phone=None, is_admin=False)
    return asyncio.run(PR.record_output_v2(req, 3, PR.RecordOutputV2Request(**body), user=user))


def test_a_save_with_only_added_articles_is_stored_under_the_change_rows_spelling(monkeypatch):
    conn = Conn()
    written = _patch(monkeypatch, conn, resolver(added=[("Salt", "rm")]))
    _call(conn, {"batch_id": 44, "rm_consumed": [{"bom_line_id": None, "material_sku_name": "salt",
                                                  "consumed_qty": 2.5}]})
    assert written[0] == ("RM", [{"bom_line_id": None, "material_sku_name": "Salt", "consumed_qty": 2.5,
                                  "remarks": None, "input_kind": None, "source_dispatch_id": None}])
    assert conn.order[:2] == ["lock", "resolver"]      # lock before the batch row is read or updated


def test_a_cleared_figure_is_saved_as_zero(monkeypatch):
    conn = Conn()
    written = _patch(monkeypatch, conn, resolver())
    _call(conn, {"batch_id": 44, "rm_consumed": [{"bom_line_id": 101, "material_sku_name": "Seeds",
                                                  "consumed_qty": 0}]})
    assert written[0][1][0]["consumed_qty"] == 0


def test_removed_articles_are_refused_before_anything_is_written(monkeypatch):
    conn = Conn()
    written = _patch(monkeypatch, conn, resolver(removed=["Pouch"]))
    with pytest.raises(HTTPException) as e:
        _call(conn, {"batch_id": 44, "pm_consumed": [{"bom_line_id": 102, "material_sku_name": "Pouch",
                                                      "consumed_qty": 5}]})
    assert e.value.status_code == 400 and e.value.detail["error"] == "article_removed_from_job_card"
    assert written == [] and "batch" not in conn.order
    with pytest.raises(HTTPException) as e:
        _call(conn, {"batch_id": 44, "balance_materials": [{"material_name": "Pouch", "balance_type": "returned",
                                                            "qty_kg": 1}]})
    assert e.value.detail["error"] == "article_removed_from_job_card"


def test_unknown_names_are_refused(monkeypatch):
    conn = Conn()
    _patch(monkeypatch, conn, resolver())
    with pytest.raises(HTTPException) as e:
        _call(conn, {"batch_id": 44, "rm_consumed": [{"bom_line_id": None, "material_sku_name": "Sugar",
                                                      "consumed_qty": 1}]})
    assert e.value.detail["error"] == "article_not_on_job_card"
```

Adjust the expected `written[0]` dict keys to exactly `ConsumedLineV2.model_dump()`'s fields in this codebase (the model also carries `remarks`, `input_kind`, `source_dispatch_id`). If `record_output_v2` touches `conn` for anything else before the consumption block (e.g. a fetch for open batches when `batch_id` is None), the tests pass an explicit `batch_id` to keep the fake small.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Append the resolver to `jc_bom_changes.py`:**

```python
# ── article checks for the write paths (spec 2d) ────────────────────────────

_CARD_BOM_SQL = "/* jcbc:card_bom */ SELECT bom_id FROM job_card_v2 WHERE job_card_id = $1"


def _removed_error(name: Optional[str]) -> dict:
    return {"error": "article_removed_from_job_card", "material_sku_name": name,
            "message": f"{(name or '').strip()} was removed from this job card's BOM."}


@dataclass
class ArticleResolver:
    master: dict[str, list[dict]]
    line_keys: dict[int, str]
    removed: dict[str, dict]
    added: dict[str, dict]

    @classmethod
    def build(cls, master_lines: Iterable[dict], changes: Changes) -> "ArticleResolver":
        master: dict[str, list[dict]] = {}
        line_keys: dict[int, str] = {}
        for l in master_lines:
            k = article_key(l.get("material_sku_name"))
            if not k:
                continue
            master.setdefault(k, []).append(dict(l))
            if l.get("bom_line_id") is not None:
                line_keys[int(l["bom_line_id"])] = k
        removed = {article_key(c["material_sku_name"]): c for c in changes.removed}
        added = {article_key(c["material_sku_name"]): c for c in changes.added
                 if article_key(c["material_sku_name"]) not in master}      # superseded -> the BOM line
        return cls(master, line_keys, removed, added)

    def state(self, name: Optional[str]) -> str:
        k = article_key(name)
        if not k:
            return "none"
        if k in self.removed:
            return "removed"
        if k in self.master:
            return "bom"
        if k in self.added:
            return "added"
        return "none"

    def is_removed(self, *, name: Optional[str] = None, bom_line_id: Any = None) -> bool:
        if bom_line_id is not None and self.line_keys.get(int(bom_line_id)) in self.removed:
            return True
        return bool(article_key(name)) and article_key(name) in self.removed

    def added_row(self, name: Optional[str]) -> Optional[dict]:
        return self.added.get(article_key(name))

    def canonical_name(self, name: Optional[str]) -> str:
        k = article_key(name)
        if k in self.master:
            return self.master[k][0]["material_sku_name"]
        if k in self.added:
            return self.added[k]["material_sku_name"]
        return (name or "").strip()

    def item_type(self, name: Optional[str]) -> Optional[str]:
        k = article_key(name)
        if k in self.master:
            return type_of(self.master[k][0].get("item_type"))
        if k in self.added:
            return type_of(self.added[k]["item_type"])
        return None

    def check_consumption(self, lines: Iterable[dict]) -> Optional[dict]:
        lines = list(lines)
        ids = {int(l["bom_line_id"]) for l in lines if l.get("bom_line_id") is not None}
        invalid = ids - set(self.line_keys)
        if invalid:
            return {"error": "invalid_bom_line",
                    "message": f"bom_line_id(s) {sorted(invalid)} do not belong to this job card's BOM"}
        for l in lines:
            name = l.get("material_sku_name")
            bid = l.get("bom_line_id")
            if bid is not None:
                k = self.line_keys[int(bid)]
                if k in self.removed:
                    return _removed_error(self.master[k][0]["material_sku_name"])
                continue
            st = self.state(name)
            if st == "removed":
                return _removed_error(name)
            if st == "none":
                return {"error": "article_not_on_job_card", "material_sku_name": name,
                        "message": f"{(name or '').strip()!r} is not on this job card's BOM."}
        return None

    def check_rows(self, *, balance: Iterable[dict], byproducts: Iterable[dict]) -> Optional[dict]:
        for r in balance:
            if float(r.get("qty_kg") or 0) <= 0:
                continue
            name = r.get("material_name") or r.get("material_sku_name")
            if article_key(name) == EGA_CONSOLIDATED:
                continue
            if self.is_removed(name=name, bom_line_id=r.get("bom_line_id")):
                return _removed_error(name)
        for r in byproducts:
            if float(r.get("qty_kg") or 0) <= 0:
                continue
            if self.is_removed(name=r.get("material_name"), bom_line_id=r.get("bom_line_id")):
                return _removed_error(r.get("material_name"))
        return None

    def stored_entry(self, entry: dict) -> dict:
        if entry.get("bom_line_id") is None:
            return {**entry, "material_sku_name": self.canonical_name(entry.get("material_sku_name"))}
        return entry


async def resolver_for(conn, job_card_id: int) -> ArticleResolver:
    bom_id = await conn.fetchval(_CARD_BOM_SQL, job_card_id)
    master = [_row(r) for r in await conn.fetch(_MASTER_SQL, bom_id)] if bom_id is not None else []
    if not master:
        master = [_indent_as_line(_row(r)) for r in await conn.fetch(_INDENTS_SQL, job_card_id)]
    changes = Changes()
    if await table_exists(conn):
        scope = await scope_of(conn, job_card_id)
        if scope is not None:
            changes = await load_changes(conn, scope.scope_job_card_id)
    return ArticleResolver.build(master, changes)
```

- [ ] **Step 4: Router edits** (CRLF):

(a) A shared validator helper near `_coerce_float`:

```python
def _coerce_line_id(v):
    """A bom_line_id of 0, below 0 or blank means "no BOM line" (an article added
    to this job card, or a client that sends 0 for none) -- never an FK value."""
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
```

(b) `ByproductLineV2` and `BalanceMaterialV2` get:

```python
    @field_validator("bom_line_id", mode="before")
    @classmethod
    def _line_id(cls, v):
        return _coerce_line_id(v)
```

(c) `ConsumedLineV2.bom_line_id: int` becomes `bom_line_id: int | None = None` with the same validator. Update its docstring: "`bom_line_id` is None for an article added to this job card (job_card_bom_change); the line is then matched by name."

(d) In `record_output_v2`, right after `_raise_if_locked(lock_err)` and **before** the batch resolution block, insert:

```python
            # Per-job-card BOM changes (spec 2d). The plan-line KEY SHARE is the
            # same row the BOM-change endpoints take FOR UPDATE, so a removal cannot
            # land between these checks and the writes below. It MUST be the first
            # row lock in this transaction (before the admin-override batch updates).
            from app.modules.production.services import jc_bom_changes as _bomc
            await _bomc.lock_line_for_card(conn, job_card_id)
            rm_rows = body.rm_consumed or []
            pm_rows = body.pm_consumed or []
            resolver = None
            if rm_rows or pm_rows or body.balance_materials or body.byproducts:
                resolver = await _bomc.resolver_for(conn, job_card_id)
                problem = resolver.check_consumption(
                    [r.model_dump() for r in rm_rows] + [p.model_dump() for p in pm_rows])
                problem = problem or resolver.check_rows(
                    balance=[b.model_dump() for b in (body.balance_materials or [])],
                    byproducts=[b.model_dump() for b in (body.byproducts or [])])
                if problem:
                    raise HTTPException(status_code=400, detail=problem)
```

(e) Replace the old consumption block (from `rm_rows = body.rm_consumed or []` through the second `upsert_consumption_lines` call) with:

```python
            # ── Per-BOM-line consumption ──────────────────────────────
            # Checked above against the job card's BOM (bom_line for its bom_id,
            # minus removed articles, plus added ones). Every stage can record
            # consumption against every article on it. The gate is "any line",
            # not "any line with a bom_line_id": an added article has none.
            if rm_rows or pm_rows:
                rec_by = user.full_name or user.phone
                await upsert_consumption_lines(
                    conn, job_card_id=job_card_id,
                    entries=[resolver.stored_entry(r.model_dump()) for r in rm_rows],
                    input_kind='RM', recorded_by=rec_by,
                    batch_id=resolved_batch_id,
                )
                await upsert_consumption_lines(
                    conn, job_card_id=job_card_id,
                    entries=[resolver.stored_entry(p.model_dump()) for p in pm_rows],
                    input_kind='PM', recorded_by=rec_by,
                    batch_id=resolved_batch_id,
                )
```

Keep the R10 comment about `None` meaning "section omitted" (move it next to the new `rm_rows`/`pm_rows` lines).

- [ ] **Step 5: Run — PASS**, then the broader router/output tests: `.venv/Scripts/python -m pytest tests/services -q -k "output or record or consumption"`. Fix any existing fake that now sees the `jcbc:lock_card_share` / `jcbc:card_bom` / `jcbc:master` queries (answer them: plan line id; the card's bom_id; the fake's BOM lines).

---

### Task S6: Writers adopt legacy no-batch rows and superseded spellings

**Files:**
- Modify: `server_replica/app/modules/production/services/job_card_v2.py` (`upsert_consumption_lines` ~487-611)
- Modify: `server_replica/app/modules/production/services/jc_accounting_v2.py` (`save_byproducts` ~690-812)
- Test: `server_replica/tests/services/test_row_adoption.py` (new)

**Interfaces:** Produces `async _adopt_consumption_row(conn, *, job_card_id, batch_id, name, bom_line_id) -> None` (job_card_v2) and `async _adopt_byproduct_row(conn, *, job_card_id, batch_id, category, material_name, bom_line_id) -> None` (jc_accounting_v2).

- [ ] **Step 1: Failing tests:**

```python
"""Save Output adopts an existing row before upserting (spec 2d 'Adopting existing rows'):
a legacy row with no batch, or a row of the same article under another spelling / with
no bom_line_id, is re-tagged instead of a second row being inserted."""
from __future__ import annotations

import asyncio

from app.modules.production.services import jc_accounting_v2 as acc
from app.modules.production.services import job_card_v2 as jcv2


class Conn:
    def __init__(self, exact=None, candidate=None):
        self.exact, self.candidate = exact, candidate
        self.sql: list[tuple[str, tuple]] = []

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.sql.append((s, args))
        if "adopt:exact" in s:
            return self.exact
        if "adopt:candidate" in s:
            return self.candidate
        raise AssertionError(s)

    async def execute(self, sql, *args):
        self.sql.append((" ".join(sql.split()), args))
        return "UPDATE 1"


def run(c):
    return asyncio.run(c)


def test_consumption_adopts_a_no_batch_or_differently_spelled_row():
    c = Conn(exact=None, candidate=555)
    run(jcv2._adopt_consumption_row(c, job_card_id=3, batch_id=44, name="Seeds", bom_line_id=101))
    upd = c.sql[-1]
    assert upd[0].startswith("UPDATE job_card_material_consumption_v2 SET batch_id = $2, material_sku_name = $3, bom_line_id = $4")
    assert upd[1] == (555, 44, "Seeds", 101)
    cand = [s for s in c.sql if "adopt:candidate" in s[0]][0]
    assert "UPPER(BTRIM(material_sku_name)) = UPPER(BTRIM($3))" in cand[0]
    assert "(batch_id = $2 OR batch_id IS NULL)" in cand[0]
    assert "ORDER BY (batch_id IS NULL), consumption_id" in cand[0]


def test_consumption_leaves_an_exact_row_alone_and_skips_without_a_batch():
    c = Conn(exact=9)
    run(jcv2._adopt_consumption_row(c, job_card_id=3, batch_id=44, name="Seeds", bom_line_id=101))
    assert not any(s.startswith("UPDATE") for s, _ in c.sql)
    c = Conn()
    run(jcv2._adopt_consumption_row(c, job_card_id=3, batch_id=None, name="Seeds", bom_line_id=None))
    assert c.sql == []


def test_byproducts_adopt_by_category_and_article():
    c = Conn(exact=None, candidate=777)
    run(acc._adopt_byproduct_row(c, job_card_id=3, batch_id=44, category="offgrade",
                                 material_name="Seeds", bom_line_id=101))
    assert c.sql[-1][1] == (777, 44, "Seeds", 101)
    cand = [s for s in c.sql if "adopt:candidate" in s[0]][0][0]
    assert "category = $3" in cand and "UPPER(BTRIM(material_name)) = UPPER(BTRIM($4))" in cand


def test_byproducts_without_an_article_adopt_the_no_article_row():
    c = Conn(exact=None, candidate=778)
    run(acc._adopt_byproduct_row(c, job_card_id=3, batch_id=44, category="control_sample",
                                 material_name=None, bom_line_id=None))
    cand = [s for s in c.sql if "adopt:candidate" in s[0]][0][0]
    assert "material_name IS NULL" in cand


def test_writers_call_the_adoption_before_upserting():
    import inspect
    up = inspect.getsource(jcv2.upsert_consumption_lines)
    assert up.index("_adopt_consumption_row(") < up.index("old_actual = await conn.fetchval(")
    sb = inspect.getsource(acc.save_byproducts)
    assert sb.index("_adopt_byproduct_row(") < sb.index("async def _insert(")
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** (CRLF files). In `job_card_v2.py`, above `upsert_consumption_lines`:

```python
async def _adopt_consumption_row(conn, *, job_card_id: int, batch_id: int | None,
                                 name: str, bom_line_id: int | None) -> None:
    """Before the upsert: when batch X has no row with this exact spelling,
    re-tag an existing row of the same article -- in batch X under another
    spelling / without a bom_line_id (an added article the BOM has since caught
    up with), else a legacy row with no batch -- so it is updated, not
    duplicated. This is what the 'promote batch_id' comment below always meant:
    the upsert key uses COALESCE(batch_id, 0), so a batch-X save never reached a
    NULL-batch row. Spec 2d."""
    if batch_id is None:
        return
    exact = await conn.fetchval(
        "/* adopt:exact */ SELECT consumption_id FROM job_card_material_consumption_v2 "
        "WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = $2 AND material_sku_name = $3",
        job_card_id, batch_id, name)
    if exact is not None:
        return
    row_id = await conn.fetchval(
        """/* adopt:candidate */
        SELECT consumption_id FROM job_card_material_consumption_v2
         WHERE job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = UPPER(BTRIM($3))
           AND (batch_id = $2 OR batch_id IS NULL)
         ORDER BY (batch_id IS NULL), consumption_id
         LIMIT 1
        """, job_card_id, batch_id, name)
    if row_id is None:
        return
    await conn.execute(
        "UPDATE job_card_material_consumption_v2 SET batch_id = $2, material_sku_name = $3, "
        "bom_line_id = $4 WHERE consumption_id = $1",
        row_id, batch_id, name, bom_line_id)
```

In `upsert_consumption_lines`, inside the per-entry loop, after `src_dispatch = e.get("source_dispatch_id")` and **before** `old_actual = await conn.fetchval(`:

```python
        await _adopt_consumption_row(conn, job_card_id=job_card_id, batch_id=batch_id,
                                     name=sku, bom_line_id=bom_line_id)
```

In `jc_accounting_v2.py`, above `save_byproducts`:

```python
async def _adopt_byproduct_row(conn, *, job_card_id: int, batch_id: int | None, category: str,
                               material_name: str | None, bom_line_id: int | None) -> None:
    """save_byproducts' twin of job_card_v2._adopt_consumption_row: re-tag an
    existing (category, article) row in batch X under another spelling, else a
    legacy no-batch row, instead of inserting a second one. Spec 2d."""
    if batch_id is None:
        return
    exact = await conn.fetchval(
        "/* adopt:exact */ SELECT byproduct_id FROM job_card_byproducts_v2 "
        "WHERE job_card_id = $1 AND COALESCE(batch_id, 0) = $2 AND category = $3 "
        "AND COALESCE(material_name, '') = COALESCE($4, '')",
        job_card_id, batch_id, category, material_name)
    if exact is not None:
        return
    if material_name:
        match = "UPPER(BTRIM(material_name)) = UPPER(BTRIM($4))"
        args = (job_card_id, batch_id, category, material_name)
    else:
        match = "material_name IS NULL"
        args = (job_card_id, batch_id, category)
    row_id = await conn.fetchval(
        f"""/* adopt:candidate */
        SELECT byproduct_id FROM job_card_byproducts_v2
         WHERE job_card_id = $1 AND category = $3 AND {match}
           AND (batch_id = $2 OR batch_id IS NULL)
         ORDER BY (batch_id IS NULL), byproduct_id
         LIMIT 1
        """, *args)
    if row_id is None:
        return
    await conn.execute(
        "UPDATE job_card_byproducts_v2 SET batch_id = $2, material_name = $3, bom_line_id = $4 "
        "WHERE byproduct_id = $1",
        row_id, batch_id, material_name, bom_line_id)
```

In `save_byproducts`, in the per-row loop after `bom_line_id` is resolved and **before** `async def _insert(`:

```python
        await _adopt_byproduct_row(conn, job_card_id=job_card_id, batch_id=batch_id, category=cat,
                                   material_name=material_name, bom_line_id=bom_line_id)
```

- [ ] **Step 4: Run — PASS**, plus existing accounting tests: `.venv/Scripts/python -m pytest tests/services -q -k "byproduct or consumption or accounting"`. Existing fakes of these two functions must answer the two new `fetchval` queries (return `None` for `adopt:exact`, `None` for `adopt:candidate` to keep today's behaviour).

---

### Task S7: Extra giveaway honours the BOM changes

**Files:**
- Modify: `server_replica/app/modules/production/services/job_card_v2.py` (`replace_balance_materials` EGA loop ~3800-3857)
- Test: `server_replica/tests/services/test_ega_bom_changes.py` (new)

- [ ] **Step 1: Failing test:**

```python
"""Extra giveaway checks the job card's BOM changes (spec 2d)."""
from __future__ import annotations

import asyncio

from app.modules.production.services import jc_bom_changes as m
from app.modules.production.services import job_card_v2 as jcv2


class Conn:
    async def fetchrow(self, sql, *a):
        # jc_meta: a packing-stage card
        return {"bom_id": 9, "stage": "packaging", "process_name": "Packaging", "output_kind": "FG"}

    async def fetchval(self, sql, *a):
        if "FROM bom_line" in sql:
            return None                     # not on the BOM module's list
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        return "DELETE 0"


def _resolver(removed=(), added=()):
    ch = m.Changes(removed=[{"change_id": 1, "change_type": "removed", "material_sku_name": n, "item_type": "rm"}
                            for n in removed],
                   added=[{"change_id": 2, "change_type": "added", "material_sku_name": n, "item_type": t}
                          for n, t in added])
    return m.ArticleResolver.build([{"bom_line_id": 101, "material_sku_name": "Seeds", "item_type": "rm"}], ch)


def _run(monkeypatch, res, name, bom_line_id=None):
    async def not_locked(c, jc):
        return None

    async def get_res(c, jc):
        return res
    monkeypatch.setattr(jcv2, "assert_not_locked", not_locked)
    monkeypatch.setattr(m, "resolver_for", get_res)
    rows = [{"balance_type": "extra_given", "material_name": name, "qty_kg": 1.0, "bom_line_id": bom_line_id}]
    return asyncio.run(jcv2.replace_balance_materials(Conn(), job_card_id=3, rows=rows, batch_id=None))


def test_added_rm_passes_the_ega_check(monkeypatch):
    out = _run(monkeypatch, _resolver(added=[("Salt", "rm")]), "Salt")
    assert out.get("error") is None


def test_added_pm_is_refused_as_non_rm(monkeypatch):
    assert _run(monkeypatch, _resolver(added=[("Tape", "pm")]), "Tape")["error"] == "ega_non_rm_material"


def test_removed_article_is_not_in_the_bom(monkeypatch):
    assert _run(monkeypatch, _resolver(removed=["Seeds"]), "Seeds", 101)["error"] == "ega_material_not_in_bom"
```

The fake answers the delete/insert path minimally; if `replace_balance_materials` needs `insert_with_pk_retry` for the insert, monkeypatch `jcv2.insert_with_pk_retry` to an async function returning `{"balance_id": 1}` and `jcv2._serialize` stays real.

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.** In `replace_balance_materials`, inside `if has_ega:` after the packing-stage gate, build the resolver once:

```python
        from app.modules.production.services import jc_bom_changes as _bomc
        bom_res = await _bomc.resolver_for(conn, job_card_id)
```

and in the per-row EGA loop, after the `CONSOLIDATED` skip, before the existing `bom_line_id = r.get("bom_line_id")` lookup:

```python
            name_for_check = r.get("material_name") or r.get("material_sku_name")
            if bom_res.is_removed(name=name_for_check, bom_line_id=r.get("bom_line_id")):
                return {
                    "error": "ega_material_not_in_bom",
                    "lookup": f"material='{name_for_check}'",
                    "message": f"{name_for_check} was removed from this job card's BOM.",
                }
            added_row = bom_res.added_row(name_for_check)
            if added_row is not None:
                item_type = added_row["item_type"]
                lookup_key = f"material='{name_for_check}'"
            else:
                # (existing bom_line_id / name lookup, unchanged, assigning item_type + lookup_key)
```

i.e. wrap the existing `if bom_line_id: … else: …` lookup in the `else:` branch so the `item_type is None` and `!= 'rm'` checks that follow apply to both.

- [ ] **Step 4: Run — PASS**, plus `-k "balance or ega"`.

---

### Task S8: Accounting record screen — returns key and conflict switch

**Files:**
- Modify: `server_replica/app/modules/production/services/jc_accounting_crud.py` (`_BALANCE` ~202, `_line_key` ~247, `_validate_lines` ~455, `_write` ~785)
- Test: `server_replica/tests/services/test_accounting_crud_balance_key.py` (new, fake conn); add two cases to `server_replica/tests/services/test_accounting_crud.py` (live Postgres, CRLF)

**Interfaces:** Produces `BALANCE_LINE_TYPE_CONFLICT: str`, `_BALANCE_115: dict`, `async _balance_spec(conn) -> dict`, `_validate_lines(incoming, balance_spec=_BALANCE)`.

- [ ] **Step 1: Failing fake-conn tests:**

```python
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
```

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** (CRLF):

```python
# Migration 115's returns index (uq_jcbm_v2_jc_batch_line_type), as the ON
# CONFLICT inference must spell it: the CASE in its own parentheses, exactly as
# the index. tests/services/test_accounting_crud_balance_key.py checks it
# against the migration text.
BALANCE_LINE_TYPE_CONFLICT = (
    "(job_card_id, COALESCE(batch_id, 0), COALESCE(bom_line_id, 0), "
    "(CASE WHEN bom_line_id IS NULL THEN UPPER(BTRIM(material_name)) ELSE '' END), "
    "balance_type)"
)


def _balance_article_part(row: dict):
    """The key's third part after 115: the article for a row with no BOM line,
    '' for a row with one (so renaming a BOM-line row stays an update). None when
    there is neither, so an empty line is still caught as 'no identifying value'."""
    if _norm("bom_line_id", row.get("bom_line_id")) is not None:
        return ""
    name = (row.get("material_name") or "").strip().upper()
    return name or None


_BALANCE_115 = {**_BALANCE, "conflict": BALANCE_LINE_TYPE_CONFLICT, "key_extra": _balance_article_part}


async def _balance_spec(conn) -> dict:
    """Which returns key applies: 115's per-article key once its index exists.
    Called after the transaction has read job_card_balance_material_v2, so its
    ACCESS SHARE lock holds the index swap off until the writes are done."""
    new = await conn.fetchval("SELECT to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NOT NULL")
    return _BALANCE_115 if new else _BALANCE
```

`_line_key`:

```python
def _line_key(spec: dict, row: dict) -> tuple:
    key = tuple(_norm(k, row.get(k)) for k in spec["key"])
    extra = spec.get("key_extra")
    return key + (extra(row),) if extra else key
```

`_validate_lines(incoming: dict, balance_spec: dict = _BALANCE)` — use `balance_spec` instead of `_BALANCE` in its section tuple. In `_write`: move `bad = _validate_lines(...)` below `stored_sections = await _fetch_sections(...)`, insert `balance_spec = await _balance_spec(conn)` between them, pass it to `_validate_lines(incoming, balance_spec)`, and use `balance_spec` for `"balance_materials"` in the tally loop. `grep -n "_BALANCE\b\|_validate_lines\|_line_key" jc_accounting_crud.py` for any other caller and thread `balance_spec` the same way (e.g. a delete/get path that builds keys).

- [ ] **Step 4: Live-Postgres cases** — append to `tests/services/test_accounting_crud.py` (CRLF; they run against the configured DATABASE_URL inside the fixture's rolled-back transaction):

```python
async def _has_115_index(conn) -> bool:
    return bool(await conn.fetchval(
        "SELECT to_regclass('public.uq_jcbm_v2_jc_batch_line_type') IS NOT NULL"))


def _two_unlinked_returns(fx: dict) -> dict:
    p = _payload(fx)
    p["balance_materials"] = [
        {"material_name": "Added Article A", "balance_type": "returned", "qty_kg": 1.0,
         "bom_line_id": None, "material_id": None, "remarks": None},
        {"material_name": "Added Article B", "balance_type": "returned", "qty_kg": 2.0,
         "bom_line_id": None, "material_id": None, "remarks": None},
    ]
    return p


@pytest.mark.asyncio
async def test_after_115_two_returns_without_a_bom_line_are_two_rows(ctx):
    conn, fx = ctx
    if not await _has_115_index(conn):
        pytest.skip("migration 115 not applied to this database")
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    created = await svc.create_record(conn, **ids, payload=_two_unlinked_returns(fx), actor="t")
    assert created.get("created"), created
    got = await svc.get_record(conn, **ids)
    assert sorted(r["qty_kg"] for r in got["balance_materials"]) == [1.0, 2.0]
    # Renaming a BOM-line row stays an update.
    p = _payload(fx)
    await svc.create_record(conn, **ids, payload=p, actor="t")
    p["balance_materials"][0]["material_name"] = fx["material"] + " (renamed)"
    res = await svc.update_record(conn, **ids, payload=p, actor="t")
    assert res["changes"]["balance_materials"]["updated"] == 1
    assert res["changes"]["balance_materials"]["inserted"] == 0


@pytest.mark.asyncio
async def test_before_115_two_returns_without_a_bom_line_are_a_duplicate(ctx):
    conn, fx = ctx
    if await _has_115_index(conn):
        pytest.skip("migration 115 already applied")
    ids = {k: fx[k] for k in ("job_card_id", "plan_id", "batch_id")}
    res = await svc.create_record(conn, **ids, payload=_two_unlinked_returns(fx), actor="t")
    assert res.get("error") == "duplicate_line"
```

(If `create_record` raises instead of returning the error dict for `duplicate_line`, assert on that shape — read `create_record` first. If the rename test's second `create_record` conflicts with the first, replace it with `update_record` of `_payload(fx)`.)

- [ ] **Step 5: Run** the new file and `test_accounting_crud.py` — PASS (the live module runs against Supabase from `.env`; the 115 case skips until the user applies 115).

---

### Task S9: Floor requisitions and receive-material

**Files:**
- Modify: `server_replica/app/modules/production/services/jc_bom_changes.py` (append `live_change_for`, `removed_keys_for`)
- Modify: `server_replica/app/modules/floor_requisition/services/requisition_service.py` (`raise_requisition` ~113-191, CRLF)
- Modify: `server_replica/app/modules/production/router.py` (`receive_material_v2` ~8675-8766)
- Test: `server_replica/tests/services/test_requisition_bom_changes.py` (new); update fakes in `tests/services/test_floor_requisition_service.py` (CRLF)

**Interfaces:** Produces `async live_change_for(conn, job_card_id, name) -> dict | None`; `async removed_keys_for(conn, job_card_id) -> set[str]`.

- [ ] **Step 1: Append to `jc_bom_changes.py`:**

```python
_LIVE_CHANGE_SQL = """/* jcbc:live_change */
    SELECT change_id, change_type, material_sku_name, item_type, sku_id, required_qty, required_unit
      FROM job_card_bom_change
     WHERE scope_job_card_id = $1 AND UPPER(BTRIM(material_sku_name)) = $2 AND undone_at IS NULL
"""

_REMOVED_KEYS_SQL = """/* jcbc:removed_keys */
    SELECT UPPER(BTRIM(material_sku_name)) AS k
      FROM job_card_bom_change
     WHERE scope_job_card_id = $1 AND change_type = 'removed' AND undone_at IS NULL
"""


async def live_change_for(conn, job_card_id: int, name: Optional[str]) -> Optional[dict]:
    """The live change for one article on this job card, or None."""
    key = article_key(name)
    if not key or not await table_exists(conn):
        return None
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        return None
    row = await conn.fetchrow(_LIVE_CHANGE_SQL, scope.scope_job_card_id, key)
    return _row(row) if row is not None else None


async def removed_keys_for(conn, job_card_id: int) -> set[str]:
    if not await table_exists(conn):
        return set()
    scope = await scope_of(conn, job_card_id)
    if scope is None:
        return set()
    return {r["k"] for r in await conn.fetch(_REMOVED_KEYS_SQL, scope.scope_job_card_id)}
```

- [ ] **Step 2: Failing tests** — `tests/services/test_requisition_bom_changes.py`. Model the fake on the existing `tests/services/test_floor_requisition_service.py` fake (read it first); monkeypatch `jc_bom_changes.lock_line_for_card` (records the call) and `jc_bom_changes.live_change_for` (returns a scripted row). Cases:
  - removed article → `RequisitionError` 422 `article_removed_from_job_card`, and no INSERT;
  - added RM article not on the BOM or indents → raised with `item_type "RM"`, unit `kg`, `required_qty` = the change's `required_qty`, shortage = required − fresh stock;
  - added PM without a required qty → unit `pcs`, required/shortage `None`;
  - the plan-line lock is called before the BOM lookup.

  And receive-material: a test calling `PR.receive_material_v2` with a fake conn (jc row, one `po_box`, one matching indent row whose `material_sku_name` is removed) and monkeypatched `removed_keys_for` → response entry `{"box_id": …, "error": "article_removed_from_job_card", "material_sku_name": …}`, no `UPDATE job_card_rm_indent_v2`, a second (not removed) box still attached, and `lock_line_for_card` called before the loop.

- [ ] **Step 3: Implement `raise_requisition`** (CRLF). After `place_scope.assert_place_allowed(...)` and before the BOM lookup:

```python
    from app.modules.production.services import jc_bom_changes
    # BOM changes (spec 2f): the plan-line lock first, then the article's live change.
    await jc_bom_changes.lock_line_for_card(conn, job_card_id)
    change = await jc_bom_changes.live_change_for(conn, job_card_id, material_sku_name) if key else None
    if change is not None and change["change_type"] == "removed":
        raise RequisitionError(422, "article_removed_from_job_card",
                               f"{(material_sku_name or '').strip()} was removed from this job card's BOM.",
                               material_sku_name=material_sku_name)
```

Then replace the `if bom is None and not indents:` refusal and the `first/article/item_type/unit` + `snap` lines with:

```python
    added = change if (change is not None and change["change_type"] == "added") else None
    if bom is None and not indents and added is None:
        raise RequisitionError(422, "article_not_on_job_card",
                               f"{(material_sku_name or '').strip()!r} is not on this job card's BOM or indents.",
                               material_sku_name=material_sku_name)

    if bom is None and not indents:
        # An article added to this job card (job_card_bom_change).
        article = added["material_sku_name"].strip()
        item_type = rules.article_key(added["item_type"]) or None
        unit = rules.unit_for(None, item_type)
        indent_lines = ([(unit, added["required_qty"], added["required_qty"])]
                        if added.get("required_qty") is not None else [])
    else:
        first = bom if bom is not None else indents[0]
        article = first["material_sku_name"].strip()
        item_type = rules.article_key(first["item_type"]) or None
        unit = rules.unit_for(indents[0]["uom"] if indents else None, item_type)
        indent_lines = [(line["uom"], line["gross_qty"], line["reqd_qty"]) for line in indents]
    qty = _qty_or_refuse(requested_qty, unit)

    stock = await floor_stock_service.fetch_floor_stock(conn, warehouse=warehouse, floor=floor)
    snap = rules.snapshot(
        unit=unit,
        indent_lines=indent_lines,
        stock=[s for s in stock["items"] if rules.article_key(s["item_name"]) == key],
    )
```

(`live_change_for` returns `required_qty` as a float via `_row`; `rules.snapshot` accepts numbers.)

- [ ] **Step 4: Implement receive-material** (router, CRLF). After the `jc` 404 check and before `attached: list[dict] = []`:

```python
            # BOM changes (spec 2g): plan-line lock before the per-box loop and
            # outside its savepoints; a removed article's box is refused per box.
            from app.modules.production.services import jc_bom_changes as _bomc
            await _bomc.lock_line_for_card(conn, job_card_id)
            removed_keys = await _bomc.removed_keys_for(conn, job_card_id)
```

Add `material_sku_name` to the indent SELECT (`SELECT rm_indent_id, scanned_box_ids, material_sku_name`) and, right after the `if not indent:` block:

```python
                        if _bomc.article_key(indent["material_sku_name"]) in removed_keys:
                            attached.append({"box_id": box_id, "error": "article_removed_from_job_card",
                                             "material_sku_name": indent["material_sku_name"]})
                            continue
```

- [ ] **Step 5: Update `tests/services/test_floor_requisition_service.py`'s fake** so its existing tests still pass: answer `/* jcbc:lock_card_share */` with the plan line id and `/* jcbc:table */` with `False` (so `live_change_for` stops there). Run: `.venv/Scripts/python -m pytest tests/services/test_requisition_bom_changes.py tests/services/test_floor_requisition_service.py tests/services/test_floor_requisition_router.py -q` — PASS.

---

### Task S10: PDF, rebuild carry-over, merge refusal

**Files:**
- Modify: `server_replica/app/modules/production/services/job_card_pdf.py` (Section 2, ~102-160)
- Modify: `server_replica/app/modules/production/services/jc_bom_changes.py` (append `repoint_after_rebuild`, `lines_with_live_changes`)
- Modify: `server_replica/app/modules/production/services/job_card_v2.py` (`replace_job_cards_for_line` ~1785, `create_merged_process_run` ~1440)
- Modify: `server_replica/app/modules/production/router.py` (merge route error mapping ~5178)
- Test: `server_replica/tests/services/test_bom_changes_pdf_rebuild.py` (new)

**Interfaces:** Produces `bom_rows(jc_data) -> list[dict]` in job_card_pdf; `async repoint_after_rebuild(conn, *, plan_line_id, new_head) -> int`; `async lines_with_live_changes(conn, plan_line_ids) -> list[str]`.

- [ ] **Step 1: Append to `jc_bom_changes.py`:**

```python
_LINE_CHANGES_SQL = """/* jcbc:line_changes */
    SELECT change_id, UPPER(BTRIM(material_sku_name)) AS k
      FROM job_card_bom_change
     WHERE plan_line_id = $1 AND undone_at IS NULL
     ORDER BY changed_at, change_id
"""

_REBUILD_UNDO_SQL = """/* jcbc:rebuild_undo */
    UPDATE job_card_bom_change
       SET undone_at = now(), undone_by = 'system', undo_reason = 'rebuild'
     WHERE change_id = ANY($1::bigint[])
"""

_REPOINT_SQL = """/* jcbc:repoint */
    UPDATE job_card_bom_change SET scope_job_card_id = $2
     WHERE plan_line_id = $1 AND undone_at IS NULL
"""

_LINES_HELD_SQL = """/* jcbc:lines_held */
    SELECT DISTINCT made_on_job_card_number
      FROM job_card_bom_change
     WHERE plan_line_id = ANY($1::bigint[]) AND undone_at IS NULL
     ORDER BY made_on_job_card_number
"""


async def repoint_after_rebuild(conn, *, plan_line_id: int, new_head: int) -> int:
    """replace_job_cards_for_line deleted every chain of the line and built one:
    carry the line's live changes over to the new chain's first card. When two old
    chains both changed one article, the earliest change is kept and the others are
    undone with undo_reason 'rebuild'. Returns how many changes carried over."""
    if not await table_exists(conn):
        return 0
    rows = await conn.fetch(_LINE_CHANGES_SQL, plan_line_id)
    keep: dict[str, int] = {}
    drop: list[int] = []
    for r in rows:
        if r["k"] in keep:
            drop.append(r["change_id"])
        else:
            keep[r["k"]] = r["change_id"]
    if drop:
        await conn.execute(_REBUILD_UNDO_SQL, drop)
    if keep:
        await conn.execute(_REPOINT_SQL, plan_line_id, new_head)
    return len(keep)


async def lines_with_live_changes(conn, plan_line_ids: list[int]) -> list[str]:
    """Job card numbers that hold live BOM changes on these plan lines."""
    if not plan_line_ids or not await table_exists(conn):
        return []
    return [r["made_on_job_card_number"] for r in await conn.fetch(_LINES_HELD_SQL, list(plan_line_ids))]
```

- [ ] **Step 2: Failing tests** — `tests/services/test_bom_changes_pdf_rebuild.py`:

```python
"""PDF rows, rebuild carry-over and merge refusal (spec 2h, 2i)."""
from __future__ import annotations

import asyncio
import inspect
import re

from app.modules.production.services import jc_bom_changes as m
from app.modules.production.services import job_card_pdf as pdf
from app.modules.production.services import job_card_v2 as jcv2

TAG = re.compile(r"/\* (jcbc:[a-z_]+) \*/")


def test_pdf_rows_drop_removed_rm_and_list_added_rm():
    jc = {"section_2a_rm_indent": [
              {"material_sku_name": "Seeds", "reqd_qty": 10, "issued_qty": 0, "batch_no": "B1", "uom": "KGS"},
              {"material_sku_name": "Old Salt", "reqd_qty": 1, "issued_qty": 0, "batch_no": "B1", "uom": "KGS"}],
          "bom_changes": {"removed": [{"material_sku_name": " old salt "}],
                          "added": [{"material_sku_name": "Sugar", "item_type": "rm", "required_qty": 2.5,
                                     "superseded": False},
                                    {"material_sku_name": "Tape", "item_type": "pm", "required_qty": 5,
                                     "superseded": False},
                                    {"material_sku_name": "Seeds", "item_type": "rm", "required_qty": 1,
                                     "superseded": True}]}}
    rows = pdf.bom_rows(jc)
    assert [r["material_sku_name"] for r in rows] == ["Seeds", "Sugar"]
    assert rows[1] == {"material_sku_name": "Sugar", "reqd_qty": 2.5, "issued_qty": None,
                       "batch_no": "", "uom": "Kgs"}


def test_pdf_rows_without_changes_are_the_indent_rows():
    jc = {"section_2a_rm_indent": [{"material_sku_name": "Seeds"}]}
    assert pdf.bom_rows(jc) == [{"material_sku_name": "Seeds"}]


class Conn:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def fetchval(self, sql, *a):
        self.calls.append((TAG.search(sql).group(1), a))
        return True

    async def fetch(self, sql, *a):
        self.calls.append((TAG.search(sql).group(1), a))
        return self.rows

    async def execute(self, sql, *a):
        self.calls.append((TAG.search(sql).group(1), a))
        return "UPDATE 1"


def test_rebuild_repoints_and_undoes_duplicates():
    conn = Conn([{"change_id": 1, "k": "SEEDS"}, {"change_id": 2, "k": "SALT"}, {"change_id": 3, "k": "SEEDS"}])
    assert asyncio.run(m.repoint_after_rebuild(conn, plan_line_id=70, new_head=900)) == 2
    tags = [t for t, _ in conn.calls]
    assert tags.index("jcbc:rebuild_undo") < tags.index("jcbc:repoint")
    assert ("jcbc:rebuild_undo", ([3],)) in conn.calls and ("jcbc:repoint", (70, 900)) in conn.calls


def test_replace_locks_the_line_first_and_repoints():
    src = inspect.getsource(jcv2.replace_job_cards_for_line)
    body = src[src.index('"""', src.index('"""') + 3) + 3:]           # after the docstring
    first_await = body.index("await ")
    assert body[first_await:].startswith("await conn.execute(") and "FOR UPDATE" in body[first_await:first_await + 200]
    assert "repoint_after_rebuild(" in src


def test_merge_is_refused_while_lines_hold_changes():
    src = inspect.getsource(jcv2.create_merged_process_run)
    assert "lines_with_live_changes(" in src and '"bom_changes_on_merged_lines"' in src
    assert src.index("FOR UPDATE OF l") < src.index("lines_with_live_changes(") < src.index("DELETE FROM job_card_v2")
```

- [ ] **Step 3: PDF** (CRLF) — add a pure helper above `generate_job_card_pdf` and use it:

```python
def bom_rows(jc_data: dict) -> list[dict]:
    """The RM rows of the printed Bill Of Material: the RM indent rows minus the
    articles removed from this job card, plus the RM articles added to it
    (job_card_bom_change; spec 2h). PM is not printed."""
    changes = jc_data.get('bom_changes') or {}
    removed = {(c.get('material_sku_name') or '').strip().upper() for c in changes.get('removed') or []}
    rows = [r for r in jc_data.get('section_2a_rm_indent', [])
            if (r.get('material_sku_name') or '').strip().upper() not in removed]
    for a in changes.get('added') or []:
        if a.get('item_type') == 'rm' and not a.get('superseded'):
            rows.append({'material_sku_name': a.get('material_sku_name'), 'reqd_qty': a.get('required_qty'),
                         'issued_qty': None, 'batch_no': '', 'uom': 'Kgs'})
    return rows
```

and replace `rm_lines = jc_data.get('section_2a_rm_indent', [])` with `rm_lines = bom_rows(jc_data)`. Check `_fmt_num(None)` returns a blank-ish string (read it); if it raises, make the added row's `issued_qty` `''` and adjust the test.

- [ ] **Step 4: Rebuild** — in `replace_job_cards_for_line` (CRLF), make the first statement after the docstring:

```python
    # Lock-order rule (spec 2c): the plan line row before any card row -- the
    # BOM-change endpoints and Save Output lock it first too.
    await conn.execute(
        "SELECT 1 FROM production_plan_line_v2 WHERE plan_line_id = $1 FOR UPDATE", plan_line_id,
    )
```

and after `result = await create_job_cards_for_line(...)` in the replace branch:

```python
    if "error" not in result:
        result["replaced"] = len(existing)
        # BOM changes carry over to the rebuilt chain (spec 2i).
        from app.modules.production.services import jc_bom_changes
        ids = result.get("job_card_ids") or []
        if ids:
            result["bom_changes_carried"] = await jc_bom_changes.repoint_after_rebuild(
                conn, plan_line_id=plan_line_id, new_head=ids[0])
```

(Keep the existing `result["replaced"]` line; do not duplicate it.)

- [ ] **Step 5: Merge** — in `create_merged_process_run`, after the `started` check (so after `FOR UPDATE OF l`) and before any delete:

```python
    # BOM changes (spec 2i): a removal carried into a shared process run would
    # apply to every member's share, so the merge waits until they are undone.
    from app.modules.production.services import jc_bom_changes
    held = await jc_bom_changes.lines_with_live_changes(conn, member_ids)
    if held:
        return {"error": "bom_changes_on_merged_lines",
                "message": ("These job cards have BOM changes: " + ", ".join(held) + ". Undo them "
                            "(Material allocation tab), merge, then make the changes on the merged run.")}
```

Router merge route: add `"bom_changes_on_merged_lines"` to the 409 tuple `("not_a_group", "already_started", "no_common_rm")`.

- [ ] **Step 6: Run — PASS**, plus `-k "pdf or replace or merge"`.

---

### Task W1: Web lib — pure rules + API client

**Files:**
- Create: `web_replica/src/lib/job-card-bom-rules.ts`, `web_replica/src/lib/job-card-bom-rules.test.ts`, `web_replica/src/lib/job-card-bom.ts`

**Interfaces (JSON from S2/S3):**
- `bom_changes`: `{scope_job_card_id, removed: [{change_id, material_sku_name, item_type: "rm"|"pm", note, changed_by, changed_at, made_on_job_card_number, not_on_bom}], added: [{…same, sku_id, required_qty: number|null, required_unit: "kg"|"pcs"|null, superseded}]}`
- write result: `{action, restored, open_requisition_ids: number[], bom_changes, bom_lines}`
- errors: HTTP status + `detail: {error, message, …}`

- [ ] **Step 1: Failing test** — `src/lib/job-card-bom-rules.test.ts`:

```ts
// node src/lib/job-card-bom-rules.test.ts
import {
  bomUnit, hasBomChanges, isRemovableType, requirementIndents, type BomChanges,
} from "./job-card-bom-rules.ts";

let failures = 0;
function check(name: string, got: unknown, want: unknown) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { failures++; console.error(`FAIL ${name}\n  got  ${g}\n  want ${w}`); }
}

const base = { note: null, changed_by: "Pat", changed_at: "2026-09-21T10:00:00+00:00", made_on_job_card_number: "PLAN-7-L1-S1" };
const changes = (removed: string[], added: Array<[string, "rm" | "pm", number | null, boolean?]>): BomChanges => ({
  scope_job_card_id: 1,
  removed: removed.map((n, i) => ({ ...base, change_id: i + 1, material_sku_name: n, item_type: "rm", not_on_bom: false })),
  added: added.map(([n, t, q, sup], i) => ({
    ...base, change_id: 100 + i, material_sku_name: n, item_type: t, sku_id: 5,
    required_qty: q, required_unit: q == null ? null : t === "pm" ? "pcs" : "kg", superseded: !!sup,
  })),
});

check("no block", hasBomChanges(undefined), false);
check("empty block", hasBomChanges(changes([], [])), false);
check("a removal", hasBomChanges(changes(["Seeds"], [])), true);

const indents = [
  { material_sku_name: "Seeds", item_type: "RM", uom: "KGS", gross_qty: 10 },
  { material_sku_name: "Salt", item_type: "RM", uom: "KGS", gross_qty: 1 },
];
check("no changes keeps the indents", requirementIndents(indents, null), indents);
check("added figure replaces the indent figure and adds new ones",
  requirementIndents(indents, changes([], [["salt", "rm", 2.5], ["Tape", "pm", 100], ["Sugar", "rm", null]])),
  [
    { material_sku_name: "Seeds", item_type: "RM", uom: "KGS", gross_qty: 10 },
    { material_sku_name: "salt", item_type: "RM", uom: "KGS", gross_qty: 2.5, reqd_qty: 2.5 },
    { material_sku_name: "Tape", item_type: "PM", uom: "PCS", gross_qty: 100, reqd_qty: 100 },
  ]);
check("superseded adds are ignored",
  requirementIndents(indents, changes([], [["Seeds", "rm", 99, true]])), indents);

check("RM removable", isRemovableType("RM"), true);
check("pm removable", isRemovableType("pm"), true);
check("SFG not removable", isRemovableType("SFG"), false);
check("unit rm", bomUnit("RM"), "kg");
check("unit pm", bomUnit("pm"), "pcs");

if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
console.log("job-card-bom-rules: all passed");
```

- [ ] **Step 2: Run** `node src/lib/job-card-bom-rules.test.ts` — FAIL (module missing).

- [ ] **Step 3: Create `src/lib/job-card-bom-rules.ts`:**

```ts
// Per-job-card BOM changes — the pure half (no React / Next imports, so it runs
// under plain Node for its tests). The server applies the changes to the job
// card's bom_lines (server_replica jc_bom_changes.py); this file only reads the
// `bom_changes` block that comes with them. Spec:
// server_replica/docs/superpowers/specs/2026-09-21-job-card-bom-changes-design.md

import { articleKey, type IndentLike } from "./floorStock.ts";

export type BomItemType = "rm" | "pm";

type BomChangeBase = {
  change_id: number;
  material_sku_name: string;
  item_type: BomItemType;
  note: string | null;
  changed_by: string;
  changed_at: string;
  made_on_job_card_number: string;
};

export type RemovedBomChange = BomChangeBase & {
  /** The article is no longer on the BOM module's list for this job card. */
  not_on_bom: boolean;
};

export type AddedBomChange = BomChangeBase & {
  sku_id: number;
  required_qty: number | null;
  required_unit: "kg" | "pcs" | null;
  /** The BOM module's list has since gained this article; the add no longer shows. */
  superseded: boolean;
};

export type BomChanges = {
  /** The job card's first stage card — the changes cover every stage of its chain. */
  scope_job_card_id: number;
  removed: RemovedBomChange[];
  added: AddedBomChange[];
};

/** Whether the server has applied changes to bom_lines. When it has, an empty
 *  bom_lines is really empty (everything removed) — never fall back to indents. */
export function hasBomChanges(c: BomChanges | null | undefined): boolean {
  return !!c && (c.removed.length > 0 || c.added.length > 0);
}

/** The lines requirementsByArticle reads: the indent lines, minus those an added
 *  article's required qty overrides, plus each added article's required qty. An
 *  added figure REPLACES an indent figure (requirementsByArticle would sum them). */
export function requirementIndents(
  indents: readonly IndentLike[],
  changes: BomChanges | null | undefined,
): IndentLike[] {
  const added = (changes?.added ?? []).filter((a) => !a.superseded && a.required_qty != null && a.required_qty > 0);
  if (added.length === 0) return [...indents];
  const keys = new Set(added.map((a) => articleKey(a.material_sku_name)));
  const out: IndentLike[] = indents.filter((i) => !keys.has(articleKey(i.material_sku_name)));
  for (const a of added) {
    out.push({
      material_sku_name: a.material_sku_name,
      item_type: a.item_type.toUpperCase(),
      uom: a.required_unit === "pcs" ? "PCS" : "KGS",
      gross_qty: a.required_qty,
      reqd_qty: a.required_qty,
    });
  }
  return out;
}

/** Only RM and PM articles can be removed; SFG is the seam a later stage consumes. */
export function isRemovableType(itemType: string | null | undefined): boolean {
  const t = (itemType ?? "").trim().toUpperCase();
  return t === "RM" || t === "PM";
}

/** An added article's required-qty unit. */
export function bomUnit(itemType: string | null | undefined): "kg" | "pcs" {
  return (itemType ?? "").trim().toUpperCase() === "PM" ? "pcs" : "kg";
}
```

- [ ] **Step 4: Create `src/lib/job-card-bom.ts`:**

```ts
// Per-job-card BOM changes API client —
// /api/v1/production/job-cards-v2/{id}/bom-changes (server_replica production router).
// Every refusal surfaces as BomChangeError carrying the server's code and message
// (e.g. article_has_records names the job card and batch holding the figures).

import { apiFetch, readApiErrorMessage } from "./auth";
import type { BomChanges } from "./job-card-bom-rules";

export class BomChangeError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "BomChangeError";
    this.code = code;
    this.status = status;
  }
}

export type BomChangeResult = {
  action: "removed" | "add_undone" | "added" | "restored";
  restored: boolean;
  open_requisition_ids: number[];
  bom_changes: BomChanges;
  bom_lines: unknown[];
};

const base = (jobCardId: number) => `/api/v1/production/job-cards-v2/${jobCardId}/bom-changes`;

async function read(res: Response, fallback: string): Promise<BomChangeResult> {
  if (res.ok) return (await res.json()) as BomChangeResult;
  const body = (await res.clone().json().catch(() => null)) as { detail?: { error?: string } } | null;
  throw new BomChangeError(await readApiErrorMessage(res, fallback), body?.detail?.error ?? "error", res.status);
}

export async function removeBomArticle(
  jobCardId: number, body: { material_sku_name: string; note?: string | null },
): Promise<BomChangeResult> {
  const res = await apiFetch(base(jobCardId), {
    method: "POST", body: JSON.stringify({ action: "remove", ...body }),
  });
  return read(res, "Couldn't remove the article.");
}

export async function addBomArticle(
  jobCardId: number, body: { sku_id: number; required_qty?: number | null; note?: string | null },
): Promise<BomChangeResult> {
  const res = await apiFetch(base(jobCardId), {
    method: "POST", body: JSON.stringify({ action: "add", ...body }),
  });
  return read(res, "Couldn't add the article.");
}

export async function undoBomChange(jobCardId: number, changeId: number): Promise<BomChangeResult> {
  const res = await apiFetch(`${base(jobCardId)}/${changeId}`, { method: "DELETE" });
  return read(res, "Couldn't undo the change.");
}
```

Check `readApiErrorMessage`'s signature in `src/lib/auth` (used the same way in `floor-requisitions.ts`) and that it reads `detail.message` for object details; if it does not, read `body.detail.message` here first.

- [ ] **Step 5: Run** the node test — PASS; `npx tsc --noEmit -p . 2>&1 | grep -v "^\.next" | grep job-card-bom` — no errors.

---

### Task W2: `outputAccounting.ts` — key matching, zero seeding, zeroing helpers

**Files:**
- Modify: `web_replica/src/app/modules/job-card/[id]/outputAccounting.ts` (CRLF)
- Create: `web_replica/src/app/modules/job-card/[id]/outputAccounting.test.ts`

**Interfaces (produced):**
- `export type ArticleKeyLike = { bom_line_id: number | null; material_sku_name: string }`
- `resolveRowKey(bomLineId, name, articles?) -> string`
- `consumptionStateFromDetail(lines, batchFilter?, articles?)` (skips rows ≤ 0)
- `balanceStateFromDetail(rows, batchFilter?, articles?)` (zero rows still seed "0")
- `rejectionsFromDetail(byproducts, balanceRows, batchFilter?)` (skips rows ≤ 0)
- `clearedConsumptionKeys(seeded, current) -> string[]`
- `clearedRejections(seeded, outgoing) -> RejectionRow[]`

- [ ] **Step 1: Failing test** — `outputAccounting.test.ts`:

```ts
// node "src/app/modules/job-card/[id]/outputAccounting.test.ts"
import {
  balanceStateFromDetail, clearedConsumptionKeys, clearedRejections, consumptionStateFromDetail,
  rejectionsFromDetail, resolveRowKey, type RejectionRow,
} from "./outputAccounting.ts";

let failures = 0;
function check(name: string, got: unknown, want: unknown) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { failures++; console.error(`FAIL ${name}\n  got  ${g}\n  want ${w}`); }
}

const articles = [
  { bom_line_id: 101, material_sku_name: "Seeds" },
  { bom_line_id: null, material_sku_name: "Salt" },
];

check("id wins", resolveRowKey(101, "whatever", articles), "b101");
check("null id resolves to the BOM line of the same article", resolveRowKey(null, " seeds ", articles), "b101");
check("null id resolves to an added article", resolveRowKey(null, "SALT", articles), "nSalt");
check("unknown stays by name", resolveRowKey(null, "Sugar", articles), "nSugar");
check("no articles: old keys", resolveRowKey(null, "Seeds"), "nSeeds");

check("consumption: superseded row lands on the BOM line; zero rows skipped",
  consumptionStateFromDetail([
    { bom_line_id: null, material_sku_name: "seeds", actual_consumed_qty: 3, batch_id: 7 },
    { bom_line_id: null, material_sku_name: "Salt", actual_consumed_qty: "0.000", batch_id: 7 },
  ], 7, articles),
  { b101: "3" });

check("balance: zero rows still seed 0, remapped",
  balanceStateFromDetail([{ bom_line_id: null, material_name: "Seeds", balance_type: "returned", qty_kg: 0, batch_id: 7 }], 7, articles),
  { b101: "0" });

check("rejections: zero rows do not come back",
  rejectionsFromDetail([
    { category: "offgrade", qty_kg: 0, material_name: "Seeds", bom_line_id: 101, batch_id: 7 },
    { category: "offgrade", qty_kg: 1.5, material_name: "Salt", bom_line_id: null, batch_id: 7 },
  ], [], 7).map((r) => r.materialName),
  ["Salt"]);

check("cleared consumption: seeded > 0 and now empty or 0",
  clearedConsumptionKeys({ b101: "3", nSalt: "1", b9: "0" }, { b101: "", nSalt: "0.5", b9: "" }),
  ["b101"]);
check("stale baseline: a figure saved elsewhere meanwhile is never zeroed",
  clearedConsumptionKeys({}, {}), []);

const row = (category: string, materialName: string, qty: string, bomLineId: number | null = null): RejectionRow =>
  ({ category, materialName, qty, remarks: "", bomLineId });
check("cleared off-grade: removed, zeroed and re-pointed rows",
  clearedRejections(
    [row("offgrade", "Seeds", "2", 101), row("tukda", "Salt", "1"), row("dust", "Sugar", "4"), row("offgrade", "", "1")],
    [row("tukda", "Salt", "0"), row("dust", "Tape", "4"), row("offgrade", "Pouch", "1")],
  ).map((r) => [r.category, r.materialName, r.qty]),
  [["offgrade", "Seeds", "0"], ["tukda", "Salt", "0"], ["dust", "Sugar", "0"]]);

if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
console.log("outputAccounting: all passed");
```

(The last case: the `("offgrade", "")` no-article row is not zeroed because the outgoing payload has an attributed `offgrade` row — `save_byproducts` deletes those itself.)

- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** (CRLF). Add near `lineKey`:

```ts
/** The fields of a catalogue article the key resolution reads. */
export type ArticleKeyLike = { bom_line_id: number | null; material_sku_name: string };

const nameKey = (n: string | null | undefined) => (n ?? "").trim().toUpperCase();

/** A saved row's grid key. A row with no bom_line_id resolves to the catalogue
 *  article of the same name (UPPER/TRIM), preferring one with a BOM line — so a
 *  figure saved for an added article still shows after the BOM module gains that
 *  article (the add is then "superseded"), and PM stays PM in the RM/PM maps. */
export function resolveRowKey(
  bomLineId: number | null | undefined,
  name: string | null | undefined,
  articles?: readonly ArticleKeyLike[],
): string {
  if (bomLineId != null) return `b${bomLineId}`;
  if (articles) {
    const k = nameKey(name);
    const matches = articles.filter((a) => nameKey(a.material_sku_name) === k);
    const withLine = matches.find((a) => a.bom_line_id != null);
    if (withLine) return `b${withLine.bom_line_id}`;
    if (matches[0]) return `n${matches[0].material_sku_name}`;
  }
  return lineKey(bomLineId, name);
}

const toNum = (v: unknown) => {
  const n = typeof v === "number" ? v : parseFloat(String(v ?? ""));
  return Number.isFinite(n) ? n : 0;
};
```

Change `consumptionStateFromDetail(lines, batchFilter = undefined, articles?: readonly ArticleKeyLike[])`: skip when `!(toNum(q) > 0)` (after the existing null/"" skip) and use `resolveRowKey(c.bom_line_id, c.material_sku_name, articles)`. Update its doc comment: zero rows are skipped so a cleared figure shows empty. Change `balanceStateFromDetail(rows, batchFilter = undefined, articles?)` to use `resolveRowKey` (zero rows unchanged). In `rejectionsFromDetail`, skip `if (!(toNum(bp.qty_kg) > 0)) continue;` with a comment ("a cleared off-grade row is saved as 0; it must not come back as a '0' row"). Add the helpers:

```ts
/** Keys to send as consumed 0: saved (as last seeded) above 0, now empty or 0.
 *  `seeded` must be what the inputs were last seeded with, not the live server
 *  memo — that one moves on every 60 s poll while the form is dirty, and would
 *  zero a figure someone else saved meanwhile. */
export function clearedConsumptionKeys(
  seeded: Record<string, string>,
  current: Record<string, string>,
): string[] {
  return Object.keys(seeded).filter((k) => toNum(seeded[k]) > 0 && !(toNum(current[k]) > 0));
}

const rejKey = (r: RejectionRow) => `${r.category}|${nameKey(r.materialName)}`;

/** Off-grade rows to send as 0: seeded (category + article) rows the operator
 *  removed, zeroed or pointed at another article. No (category, no-article)
 *  zero when the payload has an attributed row in that category —
 *  save_byproducts deletes those itself. */
export function clearedRejections(seeded: readonly RejectionRow[], outgoing: readonly RejectionRow[]): RejectionRow[] {
  const live = outgoing.filter((r) => r.category && toNum(r.qty) > 0);
  const liveKeys = new Set(live.map(rejKey));
  const attributed = new Set(live.filter((r) => r.materialName.trim()).map((r) => r.category));
  const out: RejectionRow[] = [];
  const seen = new Set<string>();
  for (const r of seeded) {
    if (!r.category || !(toNum(r.qty) > 0)) continue;
    const k = rejKey(r);
    if (liveKeys.has(k) || seen.has(k)) continue;
    if (!r.materialName.trim() && attributed.has(r.category)) continue;
    seen.add(k);
    out.push({ ...r, qty: "0" });
  }
  return out;
}
```

- [ ] **Step 4: Run — PASS.** Verify CRLF (the new test file may be LF).

---

### Task W3: Job card page — types, fallback guard, clear-to-0, props

**Files:**
- Modify: `web_replica/src/app/modules/job-card/[id]/page.tsx` (CRLF)

**Interfaces:** Consumes W1 (`hasBomChanges`, `BomChanges`) and W2. Produces `MaterialAllocationTab` props `bomChanges`, `onReload` (W5 implements them).

- [ ] **Step 1: Types.** Import `import { hasBomChanges, type BomChanges } from "@/lib/job-card-bom-rules";`. In `type BomLine` add:

```ts
  /** An article added to this job card (job_card_bom_change) — no BOM line. */
  added?: boolean;
  change_id?: number | null;
  sku_id?: number | null;
  required_qty?: number | null;
  required_unit?: string | null;
```

In `JobCardDetail` next to `bom_lines?`: `bom_changes?: BomChanges | null;` with a comment "Per-job-card BOM changes (migration 115); null before it."

- [ ] **Step 2: `computeArticles`** — change `if (bom.length > 0) {` to:

```ts
  // With BOM changes the server has already built the list (falling back to the
  // indents itself if the BOM had no lines); an empty list then means every
  // article was removed, so never fall back here.
  if (bom.length > 0 || hasBomChanges(detail.bom_changes)) {
```

- [ ] **Step 3: Articles into the state helpers.** In `computeBatchSummary`: `consumptionStateFromDetail(detail.consumption_lines, batchId, articles)` and `balanceStateFromDetail(detail.balance_materials, batchId, articles)`. In `AccountingTab`: `consumptionStateFromDetail(detail.consumption_lines, selectedBatchId, articles)` (deps add `articles`), `balanceStateFromDetail(detail.balance_materials, selectedBatchId, articles)` (deps add `articles`). Search for every other call of these two helpers in the file and pass `articles` where an articles list is in scope.

- [ ] **Step 4: Seeded baseline refs.** Next to the `consumption` / `rejections` `useState`s:

```ts
  // What the inputs were last seeded with from the server — the baseline for
  // "clearing a figure saves 0". Not the live memos: those move on every poll
  // while the form is dirty, and would zero a figure saved elsewhere meanwhile.
  const seededConsumptionRef = useRef<Record<string, string>>(consumptionFromServer);
  const seededRejectionsRef = useRef<RejectionRow[]>(rejectionsFromServer);
```

In the resync effect's microtask, before `setConsumption(consumptionFromServer);` add `seededConsumptionRef.current = consumptionFromServer;` and before `setRejections(` add `seededRejectionsRef.current = rejectionsFromServer;`. Ensure `useRef` is imported and `RejectionRow` is imported from `./outputAccounting`.

- [ ] **Step 5: Clear-to-0 in the save.** Import `clearedConsumptionKeys, clearedRejections` from `./outputAccounting`. After the `for (const a of articles)` consumption loop and before `body.rm_consumed = rmCons;`:

```ts
    // Clearing a saved figure saves 0 (W3-CRIT-2 pattern, extended to every
    // article): otherwise the old value stays on the server and the article can
    // never be removed from the job card's BOM.
    for (const k of clearedConsumptionKeys(seededConsumptionRef.current, consumption)) {
      const a = articles.find((x) => (x.bom_line_id != null ? `b${x.bom_line_id}` : `n${x.material_sku_name}`) === k);
      if (!a) continue;
      const entry = { bom_line_id: a.bom_line_id, material_sku_name: a.material_sku_name, consumed_qty: 0, uom: a.uom, input_kind: a.item_type };
      if ((a.item_type || "").toUpperCase() === "PM") pmCons.push(entry); else rmCons.push(entry);
    }
```

After the `for (const r of rejections)` byproducts loop:

```ts
    // Off-grade rows the operator removed, zeroed or re-pointed: save their old
    // (category, article) as 0 so they really clear.
    for (const r of clearedRejections(seededRejectionsRef.current, rejections)) {
      byproducts.push({
        category: r.category, qty_kg: 0, remarks: r.remarks || null,
        material_name: r.materialName || null, bom_line_id: r.bomLineId ?? null,
      });
    }
```

- [ ] **Step 6: TabPanel.** In the `case "allocation":` element add `bomChanges={detail.bom_changes ?? null}` and `onReload={onReload}`.

- [ ] **Step 7: Check** `npx tsc --noEmit -p . 2>&1 | grep -v "^\.next"` — only errors from `_MaterialAllocationTab` not yet accepting the new props (fixed in W5); `npx eslint "src/app/modules/job-card/[id]/page.tsx"`; CRLF preserved.

---

### Task W4: ArticlePicker pins the picked name's type

**Files:**
- Modify: `web_replica/src/app/modules/sample/_form.tsx` (`ArticlePicker`, search effect ~304-333, `choose` ~337-356; CRLF)

- [ ] **Step 1: Implement.** Add state `const [resultTypes, setResultTypes] = useState<Record<string, string>>({});`. In the search effect's success handler, replace the names computation:

```ts
            // Remember which allowed type each name came from, so choose() can
            // pin it: with two allowed types an unpinned lookup resolves the name
            // alone, and a name that also exists as another type (e.g. an FG of
            // the same name) could come back as the wrong SKU.
            const typeOf: Record<string, string> = {};
            lists.forEach((r, i) => {
              for (const n of r.options?.particulars ?? []) {
                if (!(n in typeOf) && types[i]) typeOf[n] = types[i] as string;
              }
            });
            const names = lists.flatMap((r) => r.options?.particulars ?? []);
            setResults(Array.from(new Set(names)).slice(0, 50));
            setResultTypes(typeOf);
```

In `choose`, change the `item_type` line to:

```ts
        item_type: itemType || singleType || (tab === "search" ? resultTypes[name] : undefined) || undefined,
```

and update the comment above it ("Pinned when a single type is allowed, the Browse type is chosen, or the Search result's own type is known"). In the error branch of the search effect also `setResultTypes({})`.

- [ ] **Step 2: Check** tsc + eslint on `_form.tsx`; CRLF preserved. (NPD's `_article-editor.tsx` uses the same picker and gets the fix; no change there.)

---

### Task W5: Material allocation tab — BOM card, Actions, dialogs, changes list

**Files:**
- Create: `web_replica/src/app/modules/job-card/[id]/_RemoveBomArticleDialog.tsx`, `_AddBomArticleDialog.tsx`, `_BomChangesList.tsx`
- Modify: `web_replica/src/app/modules/job-card/[id]/_MaterialAllocationTab.tsx` (CRLF)

**Interfaces:** Consumes W1 (`removeBomArticle`, `addBomArticle`, `undoBomChange`, `BomChangeError`, `hasBomChanges`, `requirementIndents`, `isRemovableType`, `bomUnit`, `BomChanges`), `RequisitionModal`/`BTN`/`BTN_PRIMARY`/`BTN_LINK`/`TEXTAREA`/`FIELD` from `@/components/floor-requisitions/RequisitionUi`, `checkQty` from `@/lib/floor-requisition-form`, `ArticlePicker` from `@/app/modules/sample/_form`, `friendlyApiError` from `@/lib/apiErrors`.

- [ ] **Step 1: `_RemoveBomArticleDialog.tsx`:**

```tsx
"use client";

// The ✕ on a BOM article (Material allocation tab): removes it from this job
// card's BOM — every stage of its chain — or, for an article added to the job
// card, undoes that add. The BOM module is not changed. A refusal (figures saved,
// finished job card …) shows here with the server's message.

import { useRef, useState, type FormEvent } from "react";
import { friendlyApiError } from "@/lib/apiErrors";
import { BomChangeError, removeBomArticle, type BomChangeResult } from "@/lib/job-card-bom";
import { BTN, BTN_PRIMARY, RequisitionModal, TEXTAREA } from "@/components/floor-requisitions/RequisitionUi";

export function RemoveBomArticleDialog({
  jobCardId, article, added, openRequisitionIds, onClose, onDone,
}: {
  jobCardId: number;
  article: string;
  /** An article added to this job card: the ✕ undoes the add. */
  added: boolean;
  openRequisitionIds: number[];
  onClose: () => void;
  onDone: (r: BomChangeResult) => void;
}) {
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      onDone(await removeBomArticle(jobCardId, { material_sku_name: article, note: note.trim() || null }));
    } catch (err) {
      setError(err instanceof BomChangeError ? err.message : friendlyApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <RequisitionModal title={added ? "Undo added article" : "Remove from this job card"} onClose={onClose} initialFocus={noteRef}>
      <form onSubmit={submit} className="flex flex-col gap-3 p-4 text-[13px] text-[var(--text-primary)]">
        <p>
          {added
            ? <>Take <strong>{article}</strong> off this job card&apos;s BOM (all its stages)?</>
            : <>Remove <strong>{article}</strong> from this job card&apos;s BOM (all its stages)?</>}{" "}
          The BOM in the BOM module is not changed.
        </p>
        {openRequisitionIds.length ? (
          <p className="text-[12px] text-[var(--text-secondary)]">
            {openRequisitionIds.map((id) => `Request #${id}`).join(", ")} stay{openRequisitionIds.length === 1 ? "s" : ""} open;
            Stores can still issue {openRequisitionIds.length === 1 ? "it" : "them"}.
          </p>
        ) : null}
        <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
          Note (optional)
          <textarea ref={noteRef} rows={2} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} className={TEXTAREA} />
        </label>
        {error ? <p role="alert" className="text-[12px] text-[var(--aws-error)]">{error}</p> : null}
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className={BTN} onClick={onClose}>Keep it</button>
          <button type="submit" className={BTN_PRIMARY} disabled={saving}>
            {saving ? "Removing…" : added ? "Undo add" : "Remove"}
          </button>
        </div>
      </form>
    </RequisitionModal>
  );
}
```

- [ ] **Step 2: `_AddBomArticleDialog.tsx`:**

```tsx
"use client";

// "+ Add article" (Material allocation tab): picks an RM / PM article from the
// SKU master and adds it to this job card's BOM — every stage of its chain — with
// an optional required qty (kg for RM, pcs for PM). The BOM module is not
// changed. Adding an article that was removed from this job card restores it.

import { useRef, useState, type FormEvent } from "react";
import { friendlyApiError } from "@/lib/apiErrors";
import { checkQty } from "@/lib/floor-requisition-form";
import { addBomArticle, BomChangeError, type BomChangeResult } from "@/lib/job-card-bom";
import { bomUnit, isRemovableType } from "@/lib/job-card-bom-rules";
import { ArticlePicker } from "@/app/modules/sample/_form";
import { BTN, BTN_LINK, BTN_PRIMARY, FIELD, RequisitionModal, TEXTAREA } from "@/components/floor-requisitions/RequisitionUi";

type Picked = { sku_id: number; sku_name: string; item_type?: string };

export function AddBomArticleDialog({
  jobCardId, onClose, onDone,
}: {
  jobCardId: number;
  onClose: () => void;
  onDone: (r: BomChangeResult) => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [picked, setPicked] = useState<Picked | null>(null);
  const [required, setRequired] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const type = (picked?.item_type ?? "").trim().toUpperCase();
  const typeOk = isRemovableType(type);
  const unit = bomUnit(type);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!picked || !typeOk) return;
    let qty: number | null = null;
    if (required.trim()) {
      const c = checkQty(required, unit);
      if (!c.ok) { setError(c.message); return; }
      qty = c.value;
    }
    setSaving(true);
    setError(null);
    try {
      onDone(await addBomArticle(jobCardId, { sku_id: picked.sku_id, required_qty: qty, note: note.trim() || null }));
    } catch (err) {
      setError(err instanceof BomChangeError ? err.message : friendlyApiError(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <RequisitionModal title="Add article to this job card" onClose={onClose} initialFocus={cancelRef} wide>
      <form onSubmit={submit} className="flex flex-col gap-3 p-4 text-[13px] text-[var(--text-primary)]">
        <p className="text-[12px] text-[var(--text-secondary)]">
          Adds an RM or PM article to this job card&apos;s BOM (all its stages). The BOM in the BOM module is not changed.
        </p>
        {picked ? (
          <div className="flex flex-wrap items-center gap-2 border border-[var(--aws-border)] rounded-[2px] px-2.5 py-2">
            <span className="font-medium">{picked.sku_name}</span>
            <span className="rounded border border-[var(--aws-border)] px-1.5 text-[11px] text-[var(--text-secondary)]">{type || "—"}</span>
            <button type="button" className={BTN_LINK} onClick={() => { setPicked(null); setError(null); }}>Change</button>
          </div>
        ) : (
          <ArticlePicker onAdd={(s) => { setPicked(s); setError(null); }} restrictItemType={["rm", "pm"]} />
        )}
        {picked && !typeOk ? (
          <p role="alert" className="text-[12px] text-[var(--aws-error)]">Only RM and PM articles can be added.</p>
        ) : null}
        {picked && typeOk ? (
          <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
            Required qty ({unit}, optional)
            <input className={`${FIELD} w-40`} inputMode="decimal" value={required} onChange={(e) => setRequired(e.target.value)} />
          </label>
        ) : null}
        <label className="flex flex-col gap-1 text-[12px] text-[var(--text-secondary)]">
          Note (optional)
          <textarea rows={2} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} className={TEXTAREA} />
        </label>
        {error ? <p role="alert" className="text-[12px] text-[var(--aws-error)]">{error}</p> : null}
        <div className="flex flex-wrap justify-end gap-2">
          <button ref={cancelRef} type="button" className={BTN} onClick={onClose}>Cancel</button>
          <button type="submit" className={BTN_PRIMARY} disabled={saving || !picked || !typeOk}>
            {saving ? "Adding…" : "Add"}
          </button>
        </div>
      </form>
    </RequisitionModal>
  );
}
```

Check that `ArticlePicker` is exported from `@/app/modules/sample/_form` (it is: `export function ArticlePicker`) and that `BTN_LINK`/`FIELD`/`TEXTAREA` are exported from RequisitionUi (they are).

- [ ] **Step 3: `_BomChangesList.tsx`:**

```tsx
"use client";

// "Changes on this job card": the articles removed from this job card's BOM
// (Restore), added articles the BOM module has since gained ("now on the BOM",
// Undo), and removed articles the BOM no longer lists. Collapsed by default.

import { useId, useState } from "react";
import { friendlyApiError } from "@/lib/apiErrors";
import { formatWhen } from "@/lib/floor-requisition-form";
import { BomChangeError, undoBomChange, type BomChangeResult } from "@/lib/job-card-bom";
import type { BomChanges } from "@/lib/job-card-bom-rules";
import { BTN_LINK } from "@/components/floor-requisitions/RequisitionUi";

type Item = { id: number; article: string; type: string; who: string; when: string; card: string;
  note: string | null; mark: string | null; action: "Restore" | "Undo" | null };

export function BomChangesList({
  jobCardId, changes, canEdit, onChanged,
}: {
  jobCardId: number;
  changes: BomChanges | null | undefined;
  canEdit: boolean;
  onChanged: (r: BomChangeResult) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const items: Item[] = [
    ...(changes?.removed ?? []).map((c) => ({
      id: c.change_id, article: c.material_sku_name, type: c.item_type.toUpperCase(), who: c.changed_by,
      when: c.changed_at, card: c.made_on_job_card_number, note: c.note,
      mark: c.not_on_bom ? "Removed · no longer on the BOM" : "Removed", action: "Restore" as const,
    })),
    ...(changes?.added ?? []).filter((c) => c.superseded).map((c) => ({
      id: c.change_id, article: c.material_sku_name, type: c.item_type.toUpperCase(), who: c.changed_by,
      when: c.changed_at, card: c.made_on_job_card_number, note: c.note,
      mark: "Added · now on the BOM", action: "Undo" as const,
    })),
  ];
  if (items.length === 0) return null;

  async function undo(changeId: number) {
    setBusy(changeId);
    setError(null);
    try {
      onChanged(await undoBomChange(jobCardId, changeId));
    } catch (err) {
      setError(err instanceof BomChangeError ? err.message : friendlyApiError(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-3 border-t border-[var(--aws-border)] pt-2">
      <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-controls={id}
        className="inline-flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-[var(--text-secondary)]">
        <span aria-hidden className={`inline-block text-[10px] transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
        Changes on this job card ({items.length})
      </button>
      {open ? (
        <ul id={id} className="mt-2 space-y-1.5 text-[12px]">
          {items.map((it) => (
            <li key={it.id} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="font-medium text-[var(--text-primary)]">{it.article}</span>
              <span className="text-[var(--text-secondary)]">{it.type} · {it.mark}</span>
              <span className="text-[var(--text-muted)]">by {it.who}, {formatWhen(it.when)}, on {it.card}</span>
              {it.note ? <span className="text-[var(--text-muted)] italic">“{it.note}”</span> : null}
              {canEdit && it.action ? (
                <button type="button" className={BTN_LINK} disabled={busy !== null} onClick={() => void undo(it.id)}>
                  {busy === it.id ? "…" : it.action}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {error ? <p role="alert" className="mt-1 text-[12px] text-[var(--aws-error)]">{error}</p> : null}
    </div>
  );
}
```

- [ ] **Step 4: Rework `_MaterialAllocationTab.tsx`** (CRLF). Changes, in order:

1. Header comment: add a paragraph — "The BOM articles list is the job card's own BOM: the BOM module's lines minus articles removed from this job card, plus articles added to it (job_card_bom_change, spec 2026-09-21). ✕ removes an RM/PM article; + Add article adds one; 'Changes on this job card' lists removals with Restore. The list and its controls do not depend on floor stock; only the stock cells do."
2. Imports: `hasBomChanges, isRemovableType, requirementIndents, type BomChanges` from `@/lib/job-card-bom-rules`; `type BomChangeResult` from `@/lib/job-card-bom`; the three new components.
3. Props: add `bomChanges?: BomChanges | null;` and `onReload: () => void;` (documented).
4. State: `const canEditBom = useHasPermission("production", "job_cards", "overview", "start");`, `const [removingKey, setRemovingKey] = useState<string | null>(null);`, `const [adding, setAdding] = useState(false);`.
5. `articles`: `bomLines.length || hasBomChanges(bomChanges) ? bomLines : indents.map(…)`; deps add `bomChanges`.
6. `requirements`: `requirementsByArticle(requirementIndents(indents, bomChanges))`; deps add `bomChanges`.
7. `addedKeys`: `useMemo(() => new Set((bomChanges?.added ?? []).filter((a) => !a.superseded).map((a) => articleKey(a.material_sku_name))), [bomChanges])`.
8. `bomRows`: `cover: data ? coverage(r.stock, req) : null` (no verdict without stock), deps add `data`.
9. Remove the early `if (!wh || !fl) return (…)`. Add `const hasPlace = !!wh && !!fl;`. The two fetch effects already return early without a place.
10. A `onBomChanged` callback: `const onBomChanged = useCallback((_r: BomChangeResult) => { setRemovingKey(null); setAdding(false); onReload(); reloadReqs(); }, [onReload, reloadReqs]);`
11. Render: keep the "Stock on wh · fl" header card only when `hasPlace`. Replace the `!canView ? … : err ? … : loading && !data ? … : data ? (BOM card) : null` chain with ONE always-rendered BOM card:

```tsx
      <div className={CARD}>
        <h4 className={`${HEADING} mb-2`}>BOM articles ({bomRows.length})</h4>
        {!hasPlace ? (
          <p className={`${HINT} mb-2`}>This job card has no plant or floor, so floor stock and requests are not available here.</p>
        ) : !canView && me ? (
          <p className={`${HINT} mb-2`}>Floor stock comes from the Stock Take module, which you don&apos;t have access to. Ask an admin for the Stock Take view permission.</p>
        ) : err ? (
          <p className="mb-2 text-[12px] text-[var(--aws-error)]">
            {err}{" "}
            <button type="button" onClick={() => setAttempt((n) => n + 1)} className="underline">Retry</button>
          </p>
        ) : loading && !data ? (
          <p className={`${HINT} mb-2`}>Loading floor stock…</p>
        ) : null}
        {bomRows.length === 0 ? (
          <p className={HINT}>No BOM articles on this job card.</p>
        ) : (
          /* the existing table + card list, with the changes below */
        )}
        {canEditBom ? (
          <div className="mt-3">
            <button type="button" className={BTN} onClick={() => setAdding(true)}>+ Add article</button>
          </div>
        ) : null}
        <BomChangesList jobCardId={jobCardId} changes={bomChanges} canEdit={canEditBom} onChanged={onBomChanged} />
      </div>
```

   Keep the `if (!canView && !me)` "Loading floor stock…" early return as it is (it only lasts until the profile loads).
12. Table changes: `const showActions = (canRaise && hasPlace) || canEditBom;` — header `{showActions ? <th className={TH}>Actions</th> : null}`. Article cell: `{r.article}{addedKeys.has(r.key) ? <AddedTag /> : null}` where

```tsx
function AddedTag() {
  return (
    <span className="ml-1.5 inline-block rounded bg-[#e8f1fb] px-1.5 py-0.5 text-[10px] font-semibold text-[#0b5cad] align-middle"
      title="Added to this job card; not on the BOM module's list">Added</span>
  );
}
```

   When `!data`, the stock cells for every row are one `<td className={`${TD} text-[var(--text-muted)]`} colSpan={2}>—</td>` (the existing `r.stock.length === 0` branch already renders a colSpan-2 cell; make its text `data ? "None on this floor" : "—"`). The actions cell:

```tsx
  const actionsCell = (r: (typeof bomRows)[number]) => (
    <span className="inline-flex flex-wrap items-start gap-2">
      {canRaise && hasPlace ? requestCell(r.key) : null}
      {canEditBom && isRemovableType(r.itemType) ? (
        <button
          type="button"
          className="h-8 w-8 rounded-[2px] border border-[var(--aws-border-strong)] bg-white text-[14px] leading-none text-[var(--text-secondary)] hover:border-[var(--aws-error)] hover:text-[var(--aws-error)]"
          aria-label={`Remove ${r.article} from this job card`}
          title="Remove from this job card's BOM"
          onClick={() => setRemovingKey(r.key)}
        >
          ✕
        </button>
      ) : null}
    </span>
  );
```

   Use `actionsCell(r)` for the table's `request` cell (rename to `actions`, gated on `showActions`) and for `ArticleCard`'s `action` prop.
13. Dialogs, next to the RequestDialog render:

```tsx
      {removingKey ? (() => {
        const row = bomRows.find((r) => r.key === removingKey);
        if (!row) return null;
        const open = reqState.get(removingKey)?.open;
        return (
          <RemoveBomArticleDialog
            jobCardId={jobCardId}
            article={row.article}
            added={addedKeys.has(row.key)}
            openRequisitionIds={open ? [open.requisition_id] : []}
            onClose={() => setRemovingKey(null)}
            onDone={onBomChanged}
          />
        );
      })() : null}
      {adding ? (
        <AddBomArticleDialog jobCardId={jobCardId} onClose={() => setAdding(false)} onDone={onBomChanged} />
      ) : null}
```

   (The dialog's `openRequisitionIds` is this card's open request; the server's response lists every open request on the chain and the tab reloads after it.)
14. `requestDisabled` / `RequestDialog` unchanged; `RequestDialog` still needs `wh`/`fl` — render it only when `hasPlace`.

- [ ] **Step 5: Check** — `npx tsc --noEmit -p . 2>&1 | grep -v "^\.next"` clean; `npx eslint` on the four files + page.tsx + _form.tsx + the lib files clean (no new warnings); CRLF preserved on `_MaterialAllocationTab.tsx`.

---

### Task V: Final verification

- [ ] **Step 1:** Full server suite: `cd server_replica && .venv/Scripts/python -m pytest -q` → expect 1130 + new tests passing, only the 2 known `test_sku_lookup_permission.py` failures.
- [ ] **Step 2:** Web: every `*.test.ts` touched (`node src/lib/job-card-bom-rules.test.ts`, `node "src/app/modules/job-card/[id]/outputAccounting.test.ts"`, plus `node src/lib/box-scan.test.ts` and `node src/lib/floor-requisition-form.test.ts` as regressions); `npx tsc --noEmit -p .` (ignore `.next`); `npx eslint` on every changed web file.
- [ ] **Step 3:** Line endings: for each existing file in the Global Constraints list, `grep -vc $'\r$' FILE` is 0.
- [ ] **Step 4:** `git -C server_replica status --short` and `git -C web_replica status --short` list only the files in the File map (plus the spec and this plan).
- [ ] **Step 5:** Do not commit. Report: what changed, the deploy order (server first, then 044 + 115), what could not be tested (browser, RDS, Android).


---

## Addendum A — Use on other floor stock; FG/SFG articles; stock in the Add dialog

Spec: "Addendum A" at the end of the spec. The code as it stands after Tasks S1–W5 and the review
fixes is the base (read `jc_bom_changes.py`, the router's `BomChangeBody` / `create_bom_change`,
`job_card_v2.replace_balance_materials`, `job_card_pdf.bom_rows`, `_MaterialAllocationTab.tsx`,
`_AddBomArticleDialog.tsx`, `lib/job-card-bom*.ts` first). Same Global Constraints (line endings:
check with Python byte counts — `grep` gives wrong answers in this Git Bash).

### Task A-S: server

**Files:** create `app/db/116_job_card_bom_change_types.sql`; modify `scripts/migrate.py` (after 115),
`app/modules/production/services/jc_bom_changes.py`, `app/modules/production/router.py`
(`BomChangeBody`, `create_bom_change`), `app/modules/production/services/job_card_v2.py`
(`replace_balance_materials` EGA added-row kind), `app/modules/production/services/job_card_pdf.py`
(`bom_rows`); tests: new `tests/services/test_job_card_bom_change_types_migration.py`, update
`test_jc_bom_changes.py`, `test_jc_bom_changes_writes.py`, `test_save_output_bom_changes.py`,
`test_ega_bom_changes.py`, `test_bom_changes_pdf_rebuild.py`.

1. **Migration 116** (CRLF like its siblings):

```sql
-- ===========================================================================
-- 116_job_card_bom_change_types.sql
-- job_card_bom_change may hold FG and SFG articles added to a job card (the
-- Material allocation tab's "Use" on other floor stock, and + Add article).
-- They are accounted as RM input (spec Addendum A); their required qty is kg.
-- One transaction, lock_timeout 5s; the guarded block makes a re-run a no-op.
-- MUST follow 115. Idempotent.
-- ===========================================================================

BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
BEGIN
    IF to_regclass('public.job_card_bom_change') IS NULL THEN
        RAISE NOTICE 'job_card_bom_change absent -- apply 115 first';
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.job_card_bom_change'::regclass
           AND conname = 'chk_jcbc_item_type'
           AND pg_get_constraintdef(oid) LIKE '%sfg%'
    ) THEN
        ALTER TABLE job_card_bom_change DROP CONSTRAINT IF EXISTS chk_jcbc_item_type;
        ALTER TABLE job_card_bom_change
            ADD CONSTRAINT chk_jcbc_item_type CHECK (item_type IN ('rm','pm','fg','sfg'));
        ALTER TABLE job_card_bom_change DROP CONSTRAINT IF EXISTS chk_jcbc_required;
        ALTER TABLE job_card_bom_change
            ADD CONSTRAINT chk_jcbc_required CHECK (required_qty IS NULL OR (
                change_type = 'added' AND required_qty > 0
                AND required_unit = CASE item_type WHEN 'pm' THEN 'pcs' ELSE 'kg' END
                AND (item_type <> 'pm' OR required_qty = trunc(required_qty))));
    END IF;
END $$;

COMMIT;
```

   Static test: first statement `BEGIN;`, second `SET LOCAL lock_timeout = '5s';`, last `COMMIT;`; the
   four ALTERs sit inside the `IF NOT EXISTS … LIKE '%sfg%'` block; the new CHECK texts; registered
   after 115 in `migrate.py` with the comment "116 lets job_card_bom_change hold FG/SFG articles
   (accounted as RM, required qty in kg). MUST follow 115. Idempotent."

2. **`jc_bom_changes.py`:**
   - `FG, SFG = "fg", "sfg"`; `ADDABLE_TYPES = (RM, PM, FG, SFG)`; keep `ITEM_TYPES = (RM, PM)` as the
     types a BOM-module line may be removed with; `UNIT_FOR` / `UOM_FOR` gain `fg`/`sfg` → `kg` / `KGS`.
   - `def kind_of(item_type) -> str: return PM if type_of(item_type) == PM else RM` — the accounting kind.
   - `added_line(c)`: `"item_type": kind_of(t)`, new `"article_type": t`, `"uom": UOM_FOR[kind_of(t)]`.
     `changes_payload` keeps the real type (unchanged).
   - `ArticleResolver.item_type(name)`: for an added article return `kind_of(...)`. `stored_entry(entry)`:
     for a line with no `bom_line_id` that names an added article, also set `input_kind` to
     `kind_of(type).upper()` (so an added FG is stored as `RM` consumption even if a client sends `FG`).
   - `_SKU_BY_NAME_SQL = "/* jcbc:sku_by_name */ SELECT sku_id, particulars, item_type FROM all_sku WHERE UPPER(BTRIM(particulars)) = $1 ORDER BY sku_id"`;
     `_TYPE_ORDER = (RM, PM, SFG, FG)`.
   - `add_article(conn, *, actor, job_card_id, sku_id=None, material_sku_name=None, item_type_hint=None, required_qty=None, note=None)`:
     resolve the SKU after `_open`: by `sku_id` (404 `sku_not_found` if missing), else by name — no
     rows → 404 `sku_not_found` ("X is not in the SKU master."); keep rows whose `type_of(item_type)`
     is in `ADDABLE_TYPES` (none → 422 `type_not_addable`); pick the one whose type equals
     `type_of(item_type_hint)`, else the first by `_TYPE_ORDER` then `sku_id`. A resolved SKU whose
     type is not addable → 422 `type_not_addable` ("X is PL/EGA; only RM, PM, FG and SFG articles can
     be added."). The rest of `add_article` is unchanged (restore, stale removal, already-on-list,
     `parse_required(required_qty, item_type)`, insert with the real type).
   - `remove_article`: `item_type = type_of(line.get("article_type") or line.get("item_type"))`; the
     type check applies only to BOM-module lines — `if not line.get("added") and item_type not in ITEM_TYPES:`
     refuse `not_rm_or_pm`.
   - `_insert`: also catch `asyncpg.CheckViolationError` whose constraint is `chk_jcbc_item_type` or
     `chk_jcbc_required` → `BomChangeError(503, "bom_changes_need_116", "Adding FG or SFG articles needs database migration 116, which has not been applied yet.")`.

3. **Router:** `BomChangeBody` gains `item_type: str | None = Field(default=None, max_length=20)`.
   `create_bom_change`: `add` needs `sku_id` **or** a non-blank `material_sku_name` (else 422
   `sku_required`); call `add_article(..., sku_id=body.sku_id, material_sku_name=(body.material_sku_name if body.sku_id is None else None), item_type_hint=body.item_type, ...)`.

4. **EGA** (`replace_balance_materials`): the added-row branch uses
   `item_type = jc_bom_changes.kind_of(added_row["item_type"])`, so an added FG/SFG passes as RM.

5. **PDF** `bom_rows`: list added articles whose `item_type` is not `pm` (RM, FG, SFG), same stage rule.

6. **Tests** (update the existing ones that pinned the old rule — e.g. "FG add refused" becomes "a
   `pl/ega` SKU is refused with `type_not_addable`"):
   - `effective_bom_lines`: an added `fg` line has `item_type 'rm'`, `article_type 'fg'`, `uom 'KGS'`;
     `changes_payload` still says `fg`.
   - add by sku_id of an `fg` SKU inserts `item_type 'fg'` with `required_unit 'kg'`.
   - add by name: hint `fg` picks the FG of an FG+SFG pair, hint `sfg` the SFG, no hint picks by
     `_TYPE_ORDER`; a name not in the master → 404; a `pl/ega`-only name → 422 `type_not_addable`;
     the `jcbc:sku_by_name` query gets the `UPPER(BTRIM)` key; a removed BOM line added back by name is
     restored.
   - remove: an added `sfg` article can be removed (undo); a BOM-module `sfg` line is still refused.
   - `_insert` CheckViolation on `chk_jcbc_item_type` → 503 `bom_changes_need_116`.
   - resolver: `item_type` of an added fg is `'rm'`; `stored_entry` sets `input_kind 'RM'` for it.
   - EGA: an added `fg` passes, an added `pm` is still refused.
   - PDF: an added `fg` on stage 1 is listed, an added `pm` is not.
   - routes: add by name passes `material_sku_name` and `item_type_hint`; add with neither → 422.

### Task A-W: web

**Files:** modify `src/lib/job-card-bom-rules.ts` (+ test), `src/lib/job-card-bom.ts`,
`src/lib/floorStock.ts` (`BomArticleLike` gains `article_type?: string | null`; CRLF),
`src/app/modules/job-card/[id]/page.tsx` (`BomLine` gains `article_type?: string | null`; CRLF),
`_AddBomArticleDialog.tsx`, `_MaterialAllocationTab.tsx`.

1. **`job-card-bom-rules.ts`:** `BomItemType = "rm" | "pm" | "fg" | "sfg"`; add:

```ts
/** Types that can be added to a job card (+ Add article, Use on other stock). */
export function isUsableType(itemType: string | null | undefined): boolean {
  const t = (itemType ?? "").trim().toUpperCase();
  return t === "RM" || t === "PM" || t === "FG" || t === "SFG";
}

/** An article's stock on this floor, for the Add dialog. `items` null = not loaded. */
export function stockOnFloor(
  items: readonly FloorStockLike[] | null | undefined,
  name: string,
  place: string,
): string;
```

   `stockOnFloor`: `null`/`undefined` items → `"Floor stock not loaded"`; no rows for
   `articleKey(name)` → `None on ${place}`; else `On ${place}: ` + rows (Fresh Stock first, then by
   stock type) joined with `" · "`, each `${stock_type} ${kg} kg`, kg formatted with
   `Intl.NumberFormat("en-IN", {minimumFractionDigits: 3, maximumFractionDigits: 3})`, plus
   ` (${pcs} pcs)` (whole, en-IN) when `available_quantity` is non-zero. Node tests: not loaded,
   none, one fresh row, fresh + off-grade ordering, pieces shown, case/space-insensitive match,
   `isUsableType` for rm / PM / fg / SFG / "1" / null.

2. **`job-card-bom.ts`:** `addBomArticle(jobCardId, body: { sku_id?: number; material_sku_name?: string; item_type?: string; required_qty?: number | null; note?: string | null })`.

3. **`_AddBomArticleDialog.tsx`:** new props `preset?: { name: string; itemType: string } | null`,
   `floorItems: FloorStockItem[] | null`, `place: string`. With a preset: title "Use on this job
   card", no picker, the picked block shows `preset.name` and its type; submit sends
   `{ material_sku_name: preset.name, item_type: preset.itemType.trim().toLowerCase(), required_qty, note }`.
   Without a preset: `ArticlePicker restrictItemType={["rm","pm","fg","sfg"]}`; submit sends
   `{ sku_id, required_qty, note }`. Both: under the picked article show
   `stockOnFloor(floorItems, name, place)` (muted 12px line); the type check uses `isUsableType`
   ("Only RM, PM, FG and SFG articles can be added."); required qty unit `bomUnit(type)`; intro text
   "Adds an article to this job card's BOM (all its stages)…".

4. **`_MaterialAllocationTab.tsx`:**
   - `articles` memo: map each BOM line to `{ ...l, item_type: l.article_type ?? l.item_type }` so the
     Type column shows FG/SFG for added FG/SFG (Accounting keeps `item_type 'rm'` on the page).
   - ✕ shows when `canEditBom && (isRemovableType(r.itemType) || addedKeys.has(r.key))`.
   - State `using: { name: string; itemType: string } | null`. The Add dialog renders for `adding` or
     `using`, with `preset={using}`, `floorItems={data?.items ?? null}`, and
     `place={hasPlace ? `${wh} · ${fl}` : "this floor"}`; closing and `onBomChanged` clear both.
   - Other stock table: when `canEditBom`, an **Actions** header and a cell per row holding
     `<button type="button" className={BTN} aria-label={`Use ${s.item_name} on this job card`} onClick={() => setUsing({ name: s.item_name, itemType: s.item_type ?? "" })}>Use</button>`
     only when `isUsableType(s.item_type)` (empty cell otherwise); the mobile `ArticleCard` for other
     stock gets the same button as its `action`.

5. **Checks:** node tests, `tsc`, `eslint` on every changed file, CRLF kept on `floorStock.ts` and
   `page.tsx` (Python byte counts), `_MaterialAllocationTab.tsx` stays LF.
