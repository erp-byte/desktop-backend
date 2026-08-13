# Inventory Ledger — Inward Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Inventory Ledger module's Inward column to the live legacy IMS inward tables, replacing fixtures as the default data source.

**Architecture:** A new read-only FastAPI module `app/modules/ledger/` exposes `GET /api/v1/ledger/leaves`. It unions the two legacy inward channels (`{p}_articles_v2` and `{p}_bulk_entry_articles`) per entity, aggregates in SQL by raw warehouse, then canonicalises godowns in Python and merges the collapsed rows. The frontend flips its default data source to Live and gains an `entity` field plus an "Inward only" warning chip.

**Tech Stack:** Python 3, FastAPI, asyncpg, pytest + pytest-asyncio + pglast (SQL grammar validation), Next.js 16.2.6, React 19.2.4, TypeScript.

**Spec:** `docs/superpowers/specs/2026-08-13-ledger-inward-wiring-design.md`

## Global Constraints

- **Read-only.** No INSERT/UPDATE/DELETE anywhere in this work. No migration files.
- **Entity prefixes come from a hardcoded whitelist** `("cfpl", "cdpl")` — never interpolate request input into SQL.
- **Quantities never cross UOM classes.** PM is `nos`; RM/FG is `kg`. Never sum one into the other.
- **`{p}sku` is NOT joined** — those tables hold one row each. `item_type` comes from `a.material_type` alone.
- **`rtv` / `service` filters apply to the `_v2` branch only** — those columns do not exist on `{p}_bulk_entry_transactions`, so referencing them post-union is a hard error.
- **Header↔line joins always use both `transaction_no` AND `_source`.**
- **Frontend changes are additive only.** Fixtures and the Sample/Live toggle stay in the repo.
- Backend tests run: `PYTHONPATH=. python -m pytest tests/services/<file> -v` from `server_replica/`.
- `web_replica` has **no test runner**. Frontend verification is `npx tsc --noEmit` and `npm run lint`.
- Per `web_replica/AGENTS.md`: this is Next.js 16 with breaking changes from older versions. Consult `node_modules/next/dist/docs/` before writing frontend code.

## File Structure

| File | Responsibility |
|---|---|
| `server_replica/app/modules/ledger/__init__.py` | Package marker |
| `server_replica/app/modules/ledger/services/__init__.py` | Package marker |
| `server_replica/app/modules/ledger/services/godown_alias.py` | Warehouse alias map + `ledger_godown()`. Pure, no DB. |
| `server_replica/app/modules/ledger/services/leaves_service.py` | SQL builder + fetch + Python-side canonicalise/merge |
| `server_replica/app/modules/ledger/router.py` | `GET /api/v1/ledger/leaves` |
| `server_replica/app/main.py` | Register the router |
| `server_replica/app/core/openapi_tags.py` | Swagger module registration |
| `server_replica/tests/services/test_ledger_godown_alias.py` | Alias map tests |
| `server_replica/tests/services/test_ledger_leaves.py` | SQL shape + merge tests |
| `web_replica/src/lib/ledger.ts` | `LeafItem.entity`, `LedgerApi.leaves(entity)` |
| `web_replica/src/app/modules/inventory-ledger/_LedgerData.tsx` | Default source flip |
| `web_replica/src/app/modules/inventory-ledger/_chrome.tsx` | "Inward only" chip |

---

### Task 0: Pre-flight data probe (read-only)

Spec §4 requires this before PM quantities can be trusted. It is a read-only investigation that decides whether PM inward is reported or marked unavailable. **No code is committed in this task** — it produces a decision recorded in the plan.

**Files:** none created or modified.

**Interfaces:**
- Consumes: nothing
- Produces: a yes/no decision on PM coverage that Task 2 Step 7 depends on

- [ ] **Step 1: Run the PM coverage probe**

Run against the live database (read-only):

```sql
SELECT 'cfpl' AS entity,
       lower(trim(material_type))                                    AS mt,
       count(*)                                                      AS rows,
       count(*) FILTER (WHERE quantity_units IS NULL
                           OR quantity_units = 0)                    AS missing_units,
       count(*) FILTER (WHERE net_weight IS NULL
                           OR net_weight = 0)                        AS missing_weight
  FROM cfpl_articles_v2
 GROUP BY 1, 2
UNION ALL
SELECT 'cfpl_bulk', lower(trim(material_type)), count(*),
       count(*) FILTER (WHERE quantity_units IS NULL OR quantity_units = 0),
       count(*) FILTER (WHERE net_weight IS NULL OR net_weight = 0)
  FROM cfpl_bulk_entry_articles
 GROUP BY 1, 2
UNION ALL
SELECT 'cdpl', lower(trim(material_type)), count(*),
       count(*) FILTER (WHERE quantity_units IS NULL OR quantity_units = 0),
       count(*) FILTER (WHERE net_weight IS NULL OR net_weight = 0)
  FROM cdpl_articles_v2
 GROUP BY 1, 2
UNION ALL
SELECT 'cdpl_bulk', lower(trim(material_type)), count(*),
       count(*) FILTER (WHERE quantity_units IS NULL OR quantity_units = 0),
       count(*) FILTER (WHERE net_weight IS NULL OR net_weight = 0)
  FROM cdpl_bulk_entry_articles
 GROUP BY 1, 2
 ORDER BY 1, 2;
```

- [ ] **Step 2: Run the warehouse-null probe**

```sql
SELECT 'cfpl' AS entity, count(*) AS total,
       count(*) FILTER (WHERE warehouse IS NULL OR trim(warehouse) = '') AS null_warehouse
  FROM cfpl_transactions_v2
UNION ALL
SELECT 'cdpl', count(*),
       count(*) FILTER (WHERE warehouse IS NULL OR trim(warehouse) = '')
  FROM cdpl_transactions_v2;
```

- [ ] **Step 3: Record the decision**

Append the results to this plan file under a `## Task 0 Results` heading, and record one of:

- **PM coverage GOOD** (`missing_units` is a small minority of `mt='pm'` rows) → Task 2 Step 7 keeps PM quantities.
- **PM coverage POOR** (most PM rows have NULL/zero `quantity_units`) → Task 2 Step 7 sets PM leaves to `inward_qty: None` and the service logs a warning. Do **not** emit `0` — a zero is indistinguishable from "nothing arrived".

- [ ] **Step 4: Commit the recorded results**

```bash
git add docs/superpowers/plans/2026-08-13-ledger-inward-wiring.md
git commit -m "docs: record ledger inward pre-flight probe results"
```

---

### Task 1: Godown alias map

Pure function, no database. Spec §7.

**Files:**
- Create: `server_replica/app/modules/ledger/__init__.py`
- Create: `server_replica/app/modules/ledger/services/__init__.py`
- Create: `server_replica/app/modules/ledger/services/godown_alias.py`
- Test: `server_replica/tests/services/test_ledger_godown_alias.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ledger_godown(warehouse: str | None) -> str` — always returns a non-empty string. `AMBIGUOUS_ALIASES: frozenset[str]` — normalised keys whose mapping is an inherited assumption, for logging.

- [ ] **Step 1: Write the failing test**

Create `server_replica/tests/services/test_ledger_godown_alias.py`:

```python
"""Godown canonicalisation for the Inventory Ledger.

Raw warehouse values are inconsistent across the legacy inward tables; grouping
on the raw column fragments one physical godown into many ledger rows.

Run:  PYTHONPATH=. python -m pytest tests/services/test_ledger_godown_alias.py -v
"""
from __future__ import annotations

import pytest

from app.modules.ledger.services.godown_alias import (
    AMBIGUOUS_ALIASES,
    ledger_godown,
)


@pytest.mark.parametrize("raw", [
    "savla d-39", "Savla D39", "  SAVLA D-39  ", "d39", "d-39",
    "old savla", "old_savla", "savla-d39", "savla-d-39",
    "savla d-39 cold", "savla d39 cold",
])
def test_d39_aliases_collapse(raw):
    assert ledger_godown(raw) == "Savla D-39"


@pytest.mark.parametrize("raw", [
    "savla d-514", "savla d514", "d514", "d-514", "new savla", "new_savla",
    "savla-d514", "savla-d-514", "savla d-514 cold", "savla d514 cold",
])
def test_d514_aliases_collapse(raw):
    assert ledger_godown(raw) == "Savla D-514"


@pytest.mark.parametrize("raw", ["savla bond", "SAVLA BOND", "savla_bond"])
def test_savla_bond_is_its_own_godown(raw):
    """Both legacy copies fold this into D-39. The ledger keeps it separate."""
    assert ledger_godown(raw) == "Savla Bond"


@pytest.mark.parametrize("raw,expected", [
    ("a185", "A185"), ("warehouse a185", "A185"),
    ("a-185", "A185"), ("A-185 Cold", "A185"),
    ("w202", "W202"), ("a101", "A101"), ("a68", "A68"), ("f53", "F53"),
    ("dev int", "Dev Int"), ("dev_int", "Dev Int"),
    ("rishi cold storage", "Rishi"), ("supreme cold", "Supreme"),
    ("eskimo", "Eskimo"),
])
def test_remaining_canonical_warehouses(raw, expected):
    assert ledger_godown(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_warehouse_becomes_unassigned(raw):
    """NULL warehouse is a live path: neither v2 INSERT path writes the column."""
    assert ledger_godown(raw) == "Unassigned"


def test_unknown_value_passes_through_title_cased():
    """Never drop a godown — an unmapped one must still show up in totals."""
    assert ledger_godown("some new shed") == "Some New Shed"


def test_underscore_normalisation_is_applied():
    """Legacy matching does strip().lower().replace('_',' '); dropping it breaks
    new_savla / savla_bond / dev_int."""
    assert ledger_godown("new_savla") == "Savla D-514"


def test_bare_savla_is_flagged_ambiguous():
    """Inherited from inward_tools.py and absent from canonicalize.py. With
    Savla Bond split out this is an assumption, so it must be logged."""
    assert ledger_godown("savla") == "Savla D-39"
    assert "savla" in AMBIGUOUS_ALIASES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/test_ledger_godown_alias.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.modules.ledger'`

- [ ] **Step 3: Create the package markers**

Create `server_replica/app/modules/ledger/__init__.py` (empty file).
Create `server_replica/app/modules/ledger/services/__init__.py` (empty file).

- [ ] **Step 4: Write the implementation**

Create `server_replica/app/modules/ledger/services/godown_alias.py`:

```python
"""Warehouse -> canonical godown for the Inventory Ledger.

Raw `warehouse` values in the legacy inward tables are inconsistent ('savla d-39',
'd39', 'old savla', ...). Grouping the ledger on the raw column fragments one
physical godown across many rows.

Built from legacy_backend/shared/canonicalize.py (the authoritative copy, 11
canonical warehouses), merged with the hyphen variants that only exist in
legacy_backend/services/ims_service/inward_tools.py, plus two deliberate deltas:

  1. 'savla bond' becomes its own godown. Both legacy copies fold it into
     Savla D-39; the ledger keeps it separate by requirement.
  2. 'a-185' / 'a-185 cold' are added — the hyphenated form appears in real
     inventory data and matches nothing in any legacy copy.

Deliberately NOT named canonical_warehouse(): that name is taken by an arity-2
function in legacy_backend/shared/canonicalize.py which returns None for
unrecognised values. This one takes a single value and never returns None.
"""
from __future__ import annotations

UNASSIGNED = "Unassigned"

# Keys are normalised: strip().lower().replace("_", " ")
GODOWN_ALIASES: dict[str, str] = {
    # Savla D-39
    "savla d-39": "Savla D-39",
    "savla d39": "Savla D-39",
    "savla-d39": "Savla D-39",
    "savla-d-39": "Savla D-39",
    "d-39": "Savla D-39",
    "d39": "Savla D-39",
    "old savla": "Savla D-39",
    "savla d-39 cold": "Savla D-39",
    "savla d39 cold": "Savla D-39",
    "savla": "Savla D-39",          # ambiguous — see AMBIGUOUS_ALIASES
    # Savla D-514
    "savla d-514": "Savla D-514",
    "savla d514": "Savla D-514",
    "savla-d514": "Savla D-514",
    "savla-d-514": "Savla D-514",
    "d-514": "Savla D-514",
    "d514": "Savla D-514",
    "new savla": "Savla D-514",
    "savla d-514 cold": "Savla D-514",
    "savla d514 cold": "Savla D-514",
    # Savla Bond — split out from D-39
    "savla bond": "Savla Bond",
    # Cold storages
    "rishi": "Rishi",
    "rishi cold": "Rishi",
    "rishi cold storage": "Rishi",
    "rishi cold storage pvt ltd": "Rishi",
    "supreme": "Supreme",
    "supreme cold": "Supreme",
    "supreme cold storage": "Supreme",
    "eskimo": "Eskimo",
    "eskimo cold": "Eskimo",
    "eskimo cold storage": "Eskimo",
    # Regular warehouses
    "w202": "W202",
    "warehouse w202": "W202",
    "a101": "A101",
    "warehouse a101": "A101",
    "a185": "A185",
    "warehouse a185": "A185",
    "a-185": "A185",
    "a-185 cold": "A185",
    "a68": "A68",
    "warehouse a68": "A68",
    "f53": "F53",
    "warehouse f53": "F53",
    "dev int": "Dev Int",
}

# Normalised keys whose mapping is inherited guesswork rather than confirmed.
# The service logs how many rows resolve through these so the exposure is visible.
AMBIGUOUS_ALIASES: frozenset[str] = frozenset({"savla"})


def normalise(warehouse: str | None) -> str:
    """Lowercase, trim, and treat underscores as spaces — matching legacy rules."""
    if warehouse is None:
        return ""
    return warehouse.strip().lower().replace("_", " ")


def ledger_godown(warehouse: str | None) -> str:
    """Canonical godown name. Never returns None or an empty string.

    - missing/blank      -> "Unassigned"
    - recognised alias   -> canonical name
    - anything else      -> title-cased passthrough (never dropped)
    """
    key = normalise(warehouse)
    if not key:
        return UNASSIGNED
    mapped = GODOWN_ALIASES.get(key)
    if mapped is not None:
        return mapped
    return " ".join(word.capitalize() for word in key.split())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/test_ledger_godown_alias.py -v`
Expected: PASS — all tests green.

- [ ] **Step 6: Commit**

```bash
git add app/modules/ledger/__init__.py app/modules/ledger/services/__init__.py \
        app/modules/ledger/services/godown_alias.py \
        tests/services/test_ledger_godown_alias.py
git commit -m "feat(ledger): godown canonicalisation for inward wiring"
```

---

### Task 2: Leaves service

Spec §3, §4, §5. Builds the union SQL and merges canonicalised godowns in Python.

**Files:**
- Create: `server_replica/app/modules/ledger/services/leaves_service.py`
- Test: `server_replica/tests/services/test_ledger_leaves.py`

**Interfaces:**
- Consumes: `ledger_godown`, `AMBIGUOUS_ALIASES`, `normalise` from Task 1
- Produces:
  - `ENTITIES: tuple[str, ...]` = `("cfpl", "cdpl")`
  - `build_leaves_sql(prefix: str) -> str`
  - `async fetch_leaves(conn, entity: str = "both") -> list[dict]` — returns `LeafItem`-shaped dicts

- [ ] **Step 1: Write the failing test**

Create `server_replica/tests/services/test_ledger_leaves.py`:

```python
"""Inward leaf feed for the Inventory Ledger.

The ledger unions two legacy inward channels per entity. The traps these pin:
  - referencing rtv/service after the UNION (columns absent on the bulk table)
  - joining header to lines on transaction_no alone (cross-mixes the families)
  - a GROUP BY that omits a selected column (query does not compile)
  - summing PM pieces into RM kilograms

Run:  PYTHONPATH=. python -m pytest tests/services/test_ledger_leaves.py -v
"""
from __future__ import annotations

import pytest

from app.modules.ledger.services import leaves_service as S

pglast = pytest.importorskip(
    "pglast", reason="pglast gives real PostgreSQL grammar validation; optional")


class FakeConn:
    """Captures SQL and replays canned rows."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.queries.append((sql, args))
        return list(self.rows)


def parses(sql: str) -> bool:
    pglast.parse_sql(sql)
    return True


def row(**kw):
    base = dict(
        sku_id=1, item_description="Cashew 320", item_category="Cashew Kernal",
        sub_category="Cashew", material_type="rm", warehouse_raw="d39",
        net_weight_kg=100.0, qty_units=0.0, value_indicative=1000.0,
    )
    base.update(kw)
    return base


# ── SQL shape ──────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["cfpl", "cdpl"])
def test_sql_parses(prefix):
    """Guards the defect the design review caught: a GROUP BY that omits a
    selected column does not compile."""
    assert parses(S.build_leaves_sql(prefix))


@pytest.mark.parametrize("prefix", ["cfpl", "cdpl"])
def test_sql_reads_both_inward_channels(prefix):
    sql = S.build_leaves_sql(prefix)
    assert f"{prefix}_articles_v2" in sql
    assert f"{prefix}_bulk_entry_articles" in sql
    assert f"{prefix}_transactions_v2" in sql
    assert f"{prefix}_bulk_entry_transactions" in sql


def test_sku_master_is_not_joined():
    """cfplsku / cdplsku hold one row each — joining them yields NULL."""
    sql = S.build_leaves_sql("cfpl")
    assert "cfplsku" not in sql


def test_rtv_service_filter_is_inside_the_v2_branch_only():
    """Those columns do not exist on the bulk table, so a post-union reference
    is a hard error. The predicate must sit before the UNION ALL."""
    sql = S.build_leaves_sql("cfpl")
    assert "rtv" in sql and "service" in sql
    assert sql.index("rtv") < sql.index("UNION ALL")
    assert sql.count("rtv") == 1


def test_header_line_join_uses_both_keys():
    """transaction_no alone cross-mixes the two families."""
    sql = S.build_leaves_sql("cfpl")
    joined = " ".join(sql.split())
    assert "t.transaction_no = a.transaction_no" in joined
    assert "t._source = a._source" in joined


def test_sql_selects_both_quantity_columns():
    """PM and RM quantities come from different columns and must never merge."""
    sql = S.build_leaves_sql("cfpl")
    assert "net_weight" in sql and "quantity_units" in sql


# ── Aggregation / merge behaviour ──────────────────────────────────

@pytest.mark.asyncio
async def test_entity_filter_queries_only_that_prefix():
    conn = FakeConn()
    await S.fetch_leaves(conn, entity="cdpl")
    assert len(conn.queries) == 1
    assert "cdpl_articles_v2" in conn.queries[0][0]
    assert "cfpl_articles_v2" not in conn.queries[0][0]


@pytest.mark.asyncio
async def test_both_queries_each_entity_and_stamps_the_row():
    conn = FakeConn([row()])
    out = await S.fetch_leaves(conn, entity="both")
    assert len(conn.queries) == 2
    assert {leaf["entity"] for leaf in out} == {"cfpl", "cdpl"}


@pytest.mark.asyncio
async def test_godown_aliases_merge_into_one_leaf():
    """'d39' and 'old savla' are the same physical godown; their quantities add."""
    conn = FakeConn([
        row(warehouse_raw="d39", net_weight_kg=100.0, value_indicative=1000.0),
        row(warehouse_raw="old savla", net_weight_kg=50.0, value_indicative=500.0),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert len(out) == 1
    assert out[0]["godown"] == "Savla D-39"
    assert out[0]["inward_qty"] == 150.0
    assert out[0]["value_indicative"] == 1500.0


@pytest.mark.asyncio
async def test_different_godowns_stay_separate():
    conn = FakeConn([
        row(warehouse_raw="d39"),
        row(warehouse_raw="savla bond"),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert sorted(leaf["godown"] for leaf in out) == ["Savla Bond", "Savla D-39"]


@pytest.mark.asyncio
async def test_pm_uses_piece_counts_and_nos_class():
    conn = FakeConn([row(material_type="pm", net_weight_kg=999.0, qty_units=42.0)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["uom_class"] == "nos"
    assert out[0]["inward_qty"] == 42.0


@pytest.mark.asyncio
async def test_rm_uses_weight_and_kg_class():
    conn = FakeConn([row(material_type="rm", net_weight_kg=100.0, qty_units=7.0)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["uom_class"] == "kg"
    assert out[0]["inward_qty"] == 100.0


@pytest.mark.asyncio
async def test_pm_and_rm_never_merge_even_for_one_sku_and_godown():
    conn = FakeConn([
        row(material_type="rm", net_weight_kg=100.0),
        row(material_type="pm", qty_units=42.0),
    ])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert len(out) == 2
    assert {leaf["uom_class"] for leaf in out} == {"kg", "nos"}


@pytest.mark.asyncio
async def test_null_warehouse_becomes_unassigned():
    conn = FakeConn([row(warehouse_raw=None)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["godown"] == "Unassigned"


@pytest.mark.asyncio
async def test_leaf_carries_every_field_the_frontend_reads():
    conn = FakeConn([row()])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert set(out[0]) == {
        "sku_id", "label", "item_type", "group", "subgroup", "uom_class",
        "godown", "value_indicative", "entity",
        "opening_qty", "inward_qty", "production_qty", "returns_qty",
        "consumption_qty", "outward_qty", "transfer_out_qty",
    }


@pytest.mark.asyncio
async def test_unsourced_movement_columns_are_zero():
    conn = FakeConn([row()])
    out = await S.fetch_leaves(conn, entity="cfpl")
    leaf = out[0]
    for key in ("opening_qty", "production_qty", "returns_qty",
                "consumption_qty", "outward_qty", "transfer_out_qty"):
        assert leaf[key] == 0


@pytest.mark.asyncio
async def test_unknown_entity_is_rejected():
    """Guards against a request value reaching SQL interpolation."""
    conn = FakeConn()
    with pytest.raises(ValueError):
        await S.fetch_leaves(conn, entity="cfpl; DROP TABLE x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/test_ledger_leaves.py -v`
Expected: FAIL — `ImportError: cannot import name 'leaves_service'`

- [ ] **Step 3: Write the implementation**

Create `server_replica/app/modules/ledger/services/leaves_service.py`:

```python
"""Inward leaf feed for the Inventory Ledger.

Unions the two legacy inward channels per entity:

    inward     -> {p}_transactions_v2        + {p}_articles_v2
    bulk_entry -> {p}_bulk_entry_transactions + {p}_bulk_entry_articles

Quantity comes off the ARTICLE union, not by joining boxes. A correct box join
exists ((transaction_no, _source, article_description)), but articles give one
uniform rule across both channels. Note the consequence: the ledger's bulk figure
will NOT match scripts/generate_inventory_report.py, which joins bulk boxes on
transaction_no alone and therefore multiplies weight by article count. That
divergence is the report's defect, not this one.

Godown canonicalisation happens in Python rather than SQL: the alias map stays in
one testable place, and rows whose raw warehouses collapse to the same canonical
godown are merged here.

Read-only. Nothing in this module writes.
"""
from __future__ import annotations

import logging
from typing import Any

from .godown_alias import AMBIGUOUS_ALIASES, ledger_godown, normalise

log = logging.getLogger(__name__)

# Hardcoded whitelist — request input never reaches SQL interpolation.
ENTITIES: tuple[str, ...] = ("cfpl", "cdpl")

_PM = "pm"

# Only the columns the ledger actually consumes. Explicit casts so the UNION
# survives the two families storing the same field with different types.
_ART_COLS = (
    "transaction_no::text     AS transaction_no, "
    "sku_id::bigint           AS sku_id, "
    "item_description::text   AS item_description, "
    "item_category::text      AS item_category, "
    "sub_category::text       AS sub_category, "
    "material_type::text      AS material_type, "
    "net_weight::numeric      AS net_weight, "
    "quantity_units::numeric  AS quantity_units, "
    "total_amount::numeric    AS total_amount"
)

_TX_COLS = (
    "transaction_no::text AS transaction_no, "
    "warehouse::text      AS warehouse"
)


def build_leaves_sql(prefix: str) -> str:
    """Union SQL for one entity prefix. Aggregates by RAW warehouse; the caller
    canonicalises and merges.

    The rtv/service predicate sits inside the v2 branch on purpose — those columns
    do not exist on {p}_bulk_entry_transactions, so referencing them after the
    UNION fails with `column "rtv" does not exist`.
    """
    if prefix not in ENTITIES:
        raise ValueError(f"unknown entity prefix: {prefix!r}")

    return f"""
        WITH all_tx AS (
            SELECT {_TX_COLS}, 'inward'::text AS _source
              FROM {prefix}_transactions_v2
             WHERE (rtv IS NULL OR rtv = false)
               AND (service IS NULL OR service = false)
            UNION ALL
            SELECT {_TX_COLS}, 'bulk_entry'::text AS _source
              FROM {prefix}_bulk_entry_transactions
        ),
        all_art AS (
            SELECT {_ART_COLS}, 'inward'::text AS _source
              FROM {prefix}_articles_v2
            UNION ALL
            SELECT {_ART_COLS}, 'bulk_entry'::text AS _source
              FROM {prefix}_bulk_entry_articles
        )
        SELECT a.sku_id                          AS sku_id,
               a.item_description                AS item_description,
               a.item_category                   AS item_category,
               a.sub_category                    AS sub_category,
               lower(trim(a.material_type))      AS material_type,
               t.warehouse                       AS warehouse_raw,
               COALESCE(SUM(a.net_weight), 0)    AS net_weight_kg,
               COALESCE(SUM(a.quantity_units), 0) AS qty_units,
               COALESCE(SUM(a.total_amount), 0)  AS value_indicative
          FROM all_art a
          JOIN all_tx  t
            ON t.transaction_no = a.transaction_no
           AND t._source        = a._source
         GROUP BY a.sku_id, a.item_description, a.item_category,
                  a.sub_category, lower(trim(a.material_type)), t.warehouse
    """


def _leaf_key(r: dict[str, Any], godown: str, entity: str) -> tuple:
    return (entity, r.get("sku_id"), r.get("item_description"),
            r.get("material_type"), godown)


def _to_leaf(r: dict[str, Any], godown: str, entity: str) -> dict[str, Any]:
    material_type = (r.get("material_type") or "").strip().lower()
    is_pm = material_type == _PM
    qty = r.get("qty_units") if is_pm else r.get("net_weight_kg")
    return {
        "sku_id": r.get("sku_id"),
        "label": r.get("item_description"),
        "item_type": material_type,
        "group": r.get("item_category"),
        "subgroup": r.get("sub_category"),
        "uom_class": "nos" if is_pm else "kg",
        "godown": godown,
        "entity": entity,
        "value_indicative": float(r.get("value_indicative") or 0),
        "inward_qty": float(qty or 0),
        # Not sourced in this pass. Closing is therefore NOT a stock figure —
        # the module renders an "Inward only" chip to say so.
        "opening_qty": 0,
        "production_qty": 0,
        "returns_qty": 0,
        "consumption_qty": 0,
        "outward_qty": 0,
        "transfer_out_qty": 0,
    }


async def fetch_leaves(conn, entity: str = "both") -> list[dict[str, Any]]:
    """Leaf rows for one entity or both, godowns canonicalised and merged."""
    if entity == "both":
        prefixes = ENTITIES
    elif entity in ENTITIES:
        prefixes = (entity,)
    else:
        raise ValueError(f"unknown entity: {entity!r}")

    merged: dict[tuple, dict[str, Any]] = {}
    ambiguous_rows = 0

    for prefix in prefixes:
        rows = await conn.fetch(build_leaves_sql(prefix))
        for raw in rows:
            r = dict(raw)
            raw_warehouse = r.get("warehouse_raw")
            if normalise(raw_warehouse) in AMBIGUOUS_ALIASES:
                ambiguous_rows += 1
            godown = ledger_godown(raw_warehouse)
            key = _leaf_key(r, godown, prefix)
            leaf = _to_leaf(r, godown, prefix)
            if key in merged:
                merged[key]["inward_qty"] += leaf["inward_qty"]
                merged[key]["value_indicative"] += leaf["value_indicative"]
            else:
                merged[key] = leaf

    if ambiguous_rows:
        log.warning(
            "ledger: %d inward row(s) resolved through an ambiguous godown alias "
            "(%s) — mapping is inherited, not confirmed",
            ambiguous_rows, ", ".join(sorted(AMBIGUOUS_ALIASES)),
        )

    return list(merged.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/test_ledger_leaves.py -v`
Expected: PASS — all tests green.

- [ ] **Step 5: Run the whole service suite for regressions**

Run: `PYTHONPATH=. python -m pytest tests/services -q`
Expected: no new failures versus the pre-existing baseline.

- [ ] **Step 6: Commit**

```bash
git add app/modules/ledger/services/leaves_service.py \
        tests/services/test_ledger_leaves.py
git commit -m "feat(ledger): inward leaf feed unioning both legacy channels"
```

- [ ] **Step 7: Apply the Task 0 PM decision**

If Task 0 recorded **PM coverage POOR**, change `_to_leaf` so PM leaves report unavailable rather than a misleading zero:

```python
    qty = r.get("qty_units") if is_pm else r.get("net_weight_kg")
    # PM piece counts are largely unpopulated (see Task 0 probe). Emitting 0 would
    # be indistinguishable from "nothing arrived", so report it as unknown.
    inward_qty = None if (is_pm and not qty) else float(qty or 0)
```

and set `"inward_qty": inward_qty`. Add this test to `test_ledger_leaves.py`:

```python
@pytest.mark.asyncio
async def test_pm_with_no_piece_count_reports_unknown_not_zero():
    conn = FakeConn([row(material_type="pm", qty_units=0.0, net_weight_kg=99.0)])
    out = await S.fetch_leaves(conn, entity="cfpl")
    assert out[0]["inward_qty"] is None
```

If Task 0 recorded **PM coverage GOOD**, skip this step and note that in the commit trailer.

Run: `PYTHONPATH=. python -m pytest tests/services/test_ledger_leaves.py -v`
Expected: PASS

```bash
git add app/modules/ledger/services/leaves_service.py tests/services/test_ledger_leaves.py
git commit -m "feat(ledger): report PM inward as unknown when piece counts are absent"
```

---

### Task 3: Router and registration

Spec §8.

**Files:**
- Create: `server_replica/app/modules/ledger/router.py`
- Modify: `server_replica/app/main.py`
- Modify: `server_replica/app/core/openapi_tags.py:26-57`

**Interfaces:**
- Consumes: `fetch_leaves`, `ENTITIES` from Task 2
- Produces: `router` (FastAPI `APIRouter`) exported as `ledger_router`

- [ ] **Step 1: Write the router**

Create `server_replica/app/modules/ledger/router.py`:

```python
"""GET /api/v1/ledger — read-only feed for the Inventory Ledger module.

The frontend derives every screen (stock summary tree, group drill, item hub,
ageing, FIFO) from one flat leaf feed, so this single endpoint drives the module.

Only the Inward column is sourced. The other six movement columns are zero, which
means the derived Closing is NOT a stock balance — the UI renders an "Inward only"
chip to prevent that being misread.

Read-only by design. No POST/PATCH/DELETE on this router.
"""
from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from app.modules.auth.middleware import AuthUser, get_current_user
from app.modules.ledger.services.leaves_service import ENTITIES, fetch_leaves

router = APIRouter(prefix="/api/v1/ledger", tags=["Ledger"])


@router.get("/leaves")
async def list_leaves(
    request: Request,
    entity: str = Query(
        "both",
        description="Entity scope: cfpl, cdpl, or both.",
    ),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    """Flat inward leaf rows, one per SKU x godown x material type x entity."""
    if entity not in (*ENTITIES, "both"):
        entity = "both"

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        try:
            data = await fetch_leaves(conn, entity=entity)
        except asyncpg.UndefinedTableError:
            # A legacy inward table is absent in this environment. The frontend
            # already renders an empty state; a 500 would just be noise.
            return {"data": []}
    return {"data": data}
```

- [ ] **Step 2: Register the router in main.py**

In `server_replica/app/main.py`, add the import alongside the other module router imports:

```python
from app.modules.ledger.router import router as ledger_router
```

and add the registration immediately after the `lookups_router` line (currently line 173):

```python
app.include_router(lookups_router)
app.include_router(ledger_router)
```

It must sit before the route-walking block at the end of the file, which needs to see every `include_router`.

- [ ] **Step 3: Register the Swagger module**

In `server_replica/app/core/openapi_tags.py`, add to `MODULES` (after the `"lookups"` entry):

```python
    "lookups": "Lookups",
    "ledger": "Ledger",
```

and add `"ledger"` to `MODULE_ORDER` next to `lookups`:

```python
MODULE_ORDER: list[str] = [
    "auth", "lookups", "ledger",
    "so", "purchase", "po", "receipt",
```

Note: the `tags=["Ledger"]` argument on `APIRouter` is discarded at import time; this registry is what actually names the Swagger group.

- [ ] **Step 4: Verify the app imports and the route is registered**

Run:

```bash
PYTHONPATH=. python -c "from app.main import app; print([r.path for r in app.routes if 'ledger' in r.path])"
```

Expected: `['/api/v1/ledger/leaves']`

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=. python -m pytest tests/services -q`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add app/modules/ledger/router.py app/main.py app/core/openapi_tags.py
git commit -m "feat(ledger): expose GET /api/v1/ledger/leaves"
```

---

### Task 4: Frontend wire contract

Spec §9.2. Additive type changes only.

**Files:**
- Modify: `web_replica/src/lib/ledger.ts:78-87` and `:260-262`

**Interfaces:**
- Consumes: the endpoint from Task 3
- Produces: `LeafItem.entity`, `LedgerApi.leaves(entity?, signal?)`

- [ ] **Step 1: Read the Next.js 16 guidance**

Per `web_replica/AGENTS.md`, this is not the Next.js in your training data. Before editing, skim `node_modules/next/dist/docs/01-app/` for anything affecting client components and data fetching.

- [ ] **Step 2: Add `entity` to `LeafItem`**

In `web_replica/src/lib/ledger.ts`, add the field to the `LeafItem` interface (currently lines 78-87):

```ts
export interface LeafItem extends MovementCols {
  sku_id: number;
  label: string;
  item_type: string; // rm / pm / fg
  group: string;
  subgroup: string;
  uom_class: UomClass;
  godown: string;
  value_indicative: number;
  // Which company the row came from. The header's CFPL/CDPL/Both selector
  // filters on this; without it the selector cannot do anything.
  entity: Entity;
}
```

- [ ] **Step 3: Give `leaves()` an entity argument**

Replace the `leaves` method (currently lines 260-262):

```ts
  leaves(entity: Entity | "both" = "both", signal?: AbortSignal) {
    return getJson<{ data: LeafItem[] }>(`/leaves${qs({ entity })}`, signal);
  },
```

- [ ] **Step 4: Typecheck**

Run from `web_replica/`: `npx tsc --noEmit`
Expected: no errors. `LeafItem` is constructed in `_fixtures.ts`, so a missing `entity` there will surface here — if it does, add `entity: "cfpl"` to the `leaf()` helper's returned object in `_fixtures.ts` and re-run.

- [ ] **Step 5: Commit**

```bash
git add src/lib/ledger.ts src/app/modules/inventory-ledger/_fixtures.ts
git commit -m "feat(ledger): carry entity on leaf rows"
```

---

### Task 5: Default the module to live data

Spec §9.1.

**Files:**
- Modify: `web_replica/src/app/modules/inventory-ledger/_LedgerData.tsx:5-12`, `:20`, `:49`

**Interfaces:**
- Consumes: `LedgerApi.leaves` from Task 4
- Produces: a module that reads the backend by default

- [ ] **Step 1: Flip the default**

In `web_replica/src/app/modules/inventory-ledger/_LedgerData.tsx`, change line 20:

```ts
const ENV_LIVE = process.env.NEXT_PUBLIC_LEDGER_LIVE !== "0";
```

Line 34 already derives from `ENV_LIVE` and needs no change.

- [ ] **Step 2: Update the stale comment**

Replace the final sentence of the header comment (lines 5-7) so it matches behaviour:

```ts
// The single data seam for the Inventory Ledger module. Everything the module
// shows is derived from one flat leaf set; this provider supplies it from either
// the built-in FIXTURES or the LIVE backend (GET /api/v1/ledger/leaves), chosen
// by a feature flag + a runtime toggle. LIVE is the default — set
// NEXT_PUBLIC_LEDGER_LIVE=0 (or flick the Sample/Live switch) to use fixtures.
```

- [ ] **Step 3: Pass the entity through**

Update the fetch call (currently line 49) so the new argument is explicit:

```ts
        const res = await LedgerApi.leaves("both", ac.signal);
```

- [ ] **Step 4: Typecheck and lint**

Run from `web_replica/`:

```bash
npx tsc --noEmit
npm run lint
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add src/app/modules/inventory-ledger/_LedgerData.tsx
git commit -m "feat(ledger): default the module to live backend data"
```

---

### Task 6: "Inward only" chip

Spec §9, §9.1. The chip goes in the chrome so it covers every route, not just the landing page.

**Files:**
- Modify: `web_replica/src/app/modules/inventory-ledger/_chrome.tsx`

**Interfaces:**
- Consumes: `useLedgerLeaves` from `_LedgerData.tsx`
- Produces: nothing downstream

- [ ] **Step 1: Add the chip to the chrome header**

In `web_replica/src/app/modules/inventory-ledger/_chrome.tsx`, add the import:

```ts
import { useLedgerLeaves } from "./_LedgerData";
```

Inside `LedgerChrome`, after the existing `const initial = useUserInitial();`:

```ts
  const { source } = useLedgerLeaves();
```

Then insert the chip between the `<div className="flex-1" />` spacer and the profile button:

```tsx
        <div className="flex-1" />
        {source === "live" && (
          <span
            title="Only the Inward column is wired to live data. The other movement columns are zero, so Closing is cumulative inward — not a stock balance."
            className="font-mono text-[10.5px] px-[8px] py-[3px] rounded-[6px] bg-[#fdf3e2] text-[#8a5a00] border border-[#e8c98a] whitespace-nowrap"
          >
            Inward only
          </span>
        )}
```

`LedgerChrome` renders inside `LedgerDataProvider` (mounted by `layout.tsx`), so the hook resolves on every module route.

- [ ] **Step 2: Typecheck and lint**

Run from `web_replica/`:

```bash
npx tsc --noEmit
npm run lint
```

Expected: no errors.

- [ ] **Step 3: Verify in the running app**

Run `npm run dev`, open `/modules/inventory-ledger`, and confirm:
- the chip is visible in the header while the toggle reads "Live"
- switching the toggle to "Sample" hides it
- the chip is still present after drilling into a group and into an item

- [ ] **Step 4: Commit**

```bash
git add src/app/modules/inventory-ledger/_chrome.tsx
git commit -m "feat(ledger): flag live view as inward-only across the module"
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §3 source tables, no `{p}sku` join | Task 2 (`test_sku_master_is_not_joined`) |
| §3.1 two-key join | Task 2 (`test_header_line_join_uses_both_keys`) |
| §3.2 article quantity, report divergence | Task 2 (module docstring) |
| §3.2 backfill caveat | Out of scope by §11 — operational, needs approval |
| §4 PM/RM split | Task 0 probe, Task 2 Steps 3 and 7 |
| §5 query shape, GROUP BY | Task 2 (`test_sql_parses`) |
| §5.1 rtv/service placement | Task 2 (`test_rtv_service_filter_is_inside_the_v2_branch_only`) |
| §6 LeafItem mapping | Task 2 (`test_leaf_carries_every_field_the_frontend_reads`) |
| §7 godown canonicalisation | Task 1 |
| §7.1 null handling, naming | Task 1 (`test_missing_warehouse_becomes_unassigned`) |
| §7.2 ambiguity logging | Task 1 + Task 2 (`AMBIGUOUS_ALIASES`, warning log) |
| §8 module, registration, Swagger | Task 3 |
| §9.1 default flip | Task 5 |
| §9.2 entity field | Task 4 |
| §9.3 chip in chrome | Task 6 |

No gaps.

**Deviation from the spec worth noting:** §5 wrote `ledger_godown(t.warehouse)` inside the SQL `GROUP BY`. The plan groups by **raw** warehouse in SQL and canonicalises/merges in Python instead. Same output; the alias map stays in one testable place rather than becoming a large SQL `CASE`. Task 2's merge tests pin the equivalence.

**Placeholder scan:** none — every code step carries real code, every test step a real command and expected result.

**Type consistency:** `ledger_godown` / `normalise` / `AMBIGUOUS_ALIASES` are defined in Task 1 and consumed under those exact names in Task 2. `fetch_leaves` / `ENTITIES` are defined in Task 2 and consumed in Task 3. `LeafItem.entity` is added in Task 4 and read in Task 6 via `source`, not `entity` — no collision. The leaf dict keys in Task 2 match the `LeafItem` fields in `ledger.ts` exactly, including the added `entity`.
