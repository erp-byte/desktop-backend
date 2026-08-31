"""Box identify: one planned UNION, and a missing column can never fail it.

The scan used to be a loop of point lookups, each wrapped in
`except asyncpg.PostgresError: return None`. Two things were wrong with that and
both are what these tests pin:

  * a mistyped column became a permanent, silent NOT FOUND — indistinguishable
    from a genuine miss, unlogged, in production;
  * `LIMIT 1` per table returned whichever table was probed first. `box_id` is in
    NO unique key anywhere in this schema and its 8-digit epoch base repeats about
    every 27.7 hours, so "first table wins" can report a DIFFERENT physical box as
    `found: True`.

The replacement plans the query from a cached `information_schema` read. That
buys correctness but introduces a new hazard the old design did not have: a
UNION couples every branch, so ONE missing column fails the whole statement at
PREPARE — which has already caused an outage here (`batch_number`, absent on both
tenants, once made every box lookup 500). So the load-bearing property is:

    a table that is missing or has drifted is DROPPED FROM THE PLAN,
    and the surviving branches still run.

`_plan` is pure — it takes a {table: columns} map and returns SQL — so all of
that is testable without a database at all.

Run:  PYTHONPATH=. python -m pytest tests/services/test_box_identify.py
"""
from __future__ import annotations

import asyncio
import re

import asyncpg
import pytest

from app.modules.production.services import box_identify_service as svc


# ── fixtures ─────────────────────────────────────────────────────────────────
# A schema where every branch qualifies. Column names deliberately differ per
# table, because they really do differ: `article` vs `article_description` vs
# `item_description`, `lot_no` vs `lot_number`, `weight_kg` vs `net_weight`.
def _full_schema() -> dict[str, frozenset[str]]:
    s: dict[str, frozenset[str]] = {}
    for p in ("cfpl", "cdpl"):
        s[f"{p}_boxes_v2"] = frozenset(
            {"box_id", "transaction_no", "article_description", "lot_number",
             "net_weight", "gross_weight", "count"})
        s[f"{p}_bulk_entry_boxes"] = frozenset(
            {"box_id", "transaction_no", "article_description", "lot_number",
             "net_weight", "gross_weight", "status", "count"})
        s[f"{p}_cold_stocks"] = frozenset(
            {"box_id", "transaction_no", "item_description", "lot_no", "weight_kg",
             "count"})
        s[f"{p}_rtv_boxes"] = frozenset(
            {"box_id", "header_id", "article_description", "lot_number",
             "net_weight", "gross_weight", "count"})
        s[f"{p}_rtv_header"] = frozenset({"id", "rtv_id"})
    s["interunit_transfer_boxes"] = frozenset(
        {"box_id", "transaction_no", "article", "lot_number", "net_weight",
         "gross_weight"})
    s["interunit_transfer_in_boxes"] = frozenset(
        {"box_id", "transaction_no", "article", "lot_number", "net_weight",
         "gross_weight", "original_box_id", "inward_box_id"})
    s["cold_transfer_inboxes"] = frozenset(
        {"box_id", "transaction_no", "item_description", "lot_no", "weight_kg"})
    s["jb_inward_boxes"] = frozenset(
        {"box_id", "transaction_no", "item_description", "lot_no", "net_weight",
         "gross_weight"})
    s["po_box"] = frozenset(
        {"box_id", "transaction_no", "lot_number", "net_weight", "gross_weight"})
    s["sfg_box"] = frozenset(
        {"carton_id", "fg_sku_name", "sfg_code", "net_weight", "gross_weight",
         "status", "entity", "job_card_number"})
    return s



def _arms(sql: str) -> dict[str, str]:
    """Map table name -> its UNION arm. Keyed by regex because the whole query is
    wrapped in `SELECT * FROM (`, so a naive split on "FROM " mis-keys arm one."""
    out = {}
    for arm in sql.split(" UNION ALL "):
        m = re.search(r"FROM (\w+) b", arm)
        if m:
            out[m.group(1)] = arm
    return out


@pytest.fixture(autouse=True)
def _clear_cache():
    svc._COLUMNS.clear()
    svc._WARNED.clear()
    yield
    svc._COLUMNS.clear()
    svc._WARNED.clear()


# ── the core guarantee: a broken table cannot break the others ───────────────
def test_missing_column_drops_only_that_branch():
    """The PREPARE-poisoning guard. This is the whole point of planning."""
    schema = _full_schema()
    # transaction_no disappears — an IDENTITY column, so the branch cannot run.
    schema["interunit_transfer_boxes"] = frozenset({"box_id", "article", "lot_number"})

    sql, sources = svc._plan(schema, with_txn=True)

    assert "interunit_transfer_boxes" not in sources
    # ...and everything else still runs.
    assert "cfpl_boxes_v2" in sources
    assert "interunit_transfer_in_boxes" in sources
    assert "jb_inward_boxes" in sources
    # The dropped table must not be referenced anywhere in the SQL, or the
    # statement would still fail at PREPARE.
    assert "FROM interunit_transfer_boxes" not in sql


def test_missing_table_drops_only_that_branch():
    schema = _full_schema()
    del schema["jb_inward_boxes"]
    sql, sources = svc._plan(schema, with_txn=True)
    assert "jb_inward_boxes" not in sources
    assert "FROM jb_inward_boxes" not in sql
    assert "cfpl_boxes_v2" in sources


def test_empty_schema_plans_nothing_rather_than_broken_sql():
    sql, sources = svc._plan({}, with_txn=True)
    assert sql == ""
    assert sources == []


def test_plan_asks_for_two_rows_so_ambiguity_is_detectable():
    """LIMIT 2, not 1. The second row is never returned to the caller — it exists
    only so a multi-table match can be reported instead of silently resolved.
    A fake connection hands back whatever it likes regardless of the LIMIT, so
    this has to be asserted on the SQL itself or the property is untested."""
    sql, _ = svc._plan(_full_schema(), with_txn=True)
    assert sql.rstrip().endswith("LIMIT 2")
    assert "ORDER BY _prio" in sql, "an unordered LIMIT picks an arbitrary box"


def test_every_branch_projects_the_same_columns():
    """UNION arms must line up. A width mismatch is a runtime 500, not a bad row."""
    sql, sources = svc._plan(_full_schema(), with_txn=False)
    arms = sql.split(" UNION ALL ")
    assert len(arms) == len(sources) > 10
    shapes = {tuple(a.split(" AS ")[1:][i].split(",")[0].split(" ")[0]
                    for i in range(len(a.split(" AS ")) - 1)) for a in arms}
    assert len(shapes) == 1, f"branches disagree on projection: {shapes}"


# ── the RTV join: its document number is on the header, not the box ─────────
def test_rtv_branch_joins_header_for_the_transaction_number():
    sql, sources = svc._plan(_full_schema(), with_txn=True)
    assert "cfpl_rtv_boxes" in sources
    assert "JOIN cfpl_rtv_header h ON b.header_id = h.id" in sql
    # The document number comes off the header, never off the box row.
    assert "h.rtv_id::text AS transaction_no" in sql


def test_rtv_branch_needs_both_tables():
    schema = _full_schema()
    del schema["cfpl_rtv_header"]
    _sql, sources = svc._plan(schema, with_txn=True)
    assert "cfpl_rtv_boxes" not in sources     # box table alone is unusable
    assert "cdpl_rtv_boxes" in sources         # the other entity is unaffected


# ── relabelled boxes carry more than one identity ───────────────────────────
def test_transfer_in_matches_alternate_box_identities():
    """A relabelled box keeps its old id in original_box_id. Matching box_id
    alone silently misses every box that was relabelled on receive."""
    sql, _ = svc._plan(_full_schema(), with_txn=True)
    arm = [a for a in sql.split(" UNION ALL ")
           if "FROM interunit_transfer_in_boxes" in a][0]
    assert "b.box_id::text = $1" in arm
    assert "b.original_box_id::text = $1" in arm
    assert "b.inward_box_id::text = $1" in arm


def test_alternate_identity_columns_are_omitted_when_absent():
    """inward_box_id exists in one environment and not another — referencing it
    blindly would fail the whole UNION at PREPARE."""
    schema = _full_schema()
    schema["interunit_transfer_in_boxes"] = frozenset(
        {"box_id", "transaction_no", "article", "lot_number"})
    sql, sources = svc._plan(schema, with_txn=True)
    assert "interunit_transfer_in_boxes" in sources   # still usable
    arm = [a for a in sql.split(" UNION ALL ")
           if "FROM interunit_transfer_in_boxes" in a][0]
    assert "b.box_id::text = $1" in arm
    assert "original_box_id" not in arm
    assert "inward_box_id" not in arm


# ── sfg_box has no transaction number ───────────────────────────────────────
def test_sfg_box_excluded_from_the_transaction_pass():
    _sql, sources = svc._plan(_full_schema(), with_txn=True)
    assert "sfg_box" not in sources


def test_sfg_box_included_in_the_box_id_pass_on_carton_id():
    sql, sources = svc._plan(_full_schema(), with_txn=False)
    assert "sfg_box" in sources
    arm = [a for a in sql.split(" UNION ALL ") if "FROM sfg_box" in a][0]
    assert "b.carton_id::text = $1" in arm
    assert "$2" not in arm


# ── prefix is an ordering hint, never a filter ──────────────────────────────
@pytest.mark.parametrize("txn,expect", [
    ("BE-20260101120000", "bulk_entry_boxes"),
    ("CR-20260101120000", "rtv_boxes"),
    # The rename was forward-only: both prefixes are live on printed labels and
    # must resolve to the same branch.
    ("RTV-20260101120000", "rtv_boxes"),
    ("TR-20260101120000", None),
    ("", None),
    (None, None),
])
def test_prefix_hint_mapping(txn, expect):
    assert svc._hinted_first(txn) == expect


def test_hint_reorders_but_never_excludes():
    """TR- is minted by seven generators into nine table families, so a prefix
    can never select a table — only float one up the ordering."""
    plain, src_plain = svc._plan(_full_schema(), with_txn=True)
    hinted, src_hinted = svc._plan(_full_schema(), with_txn=True, hint="rtv_boxes")
    assert set(src_plain) == set(src_hinted)      # same tables, always
    assert src_hinted[0] == "cfpl_rtv_boxes"      # just reordered
    assert src_plain[0] != "cfpl_rtv_boxes"
    assert plain != hinted


# ── per-table column quirks that were previously papered over by aliases ────
def test_count_is_never_surfaced_as_a_per_box_figure():
    """`count` is an ARTICLE total replicated onto every box row — inward_tools.py
    writes `"count": article.box_count` inside `for box_num in range(...)`, and
    bulk_entry_service does the identical thing. Surfacing it would show one
    carton as e.g. 1403. The fixture DOES declare the column on all three tables,
    so a NULL here can only come from the spec — without that this test passes
    vacuously."""
    schema = _full_schema()
    for t in ("cfpl_boxes_v2", "cfpl_bulk_entry_boxes", "cfpl_cold_stocks"):
        assert "count" in schema[t], "fixture must declare the column it guards"
    sql, _ = svc._plan(schema, with_txn=True)
    arms = _arms(sql)
    for t in ("cfpl_boxes_v2", "cfpl_bulk_entry_boxes", "cfpl_cold_stocks"):
        assert "NULL::bigint AS count" in arms[t], t


def test_boxes_v2_has_no_status_column():
    sql, _ = svc._plan(_full_schema(), with_txn=True)
    v2 = [a for a in sql.split(" UNION ALL ") if "FROM cfpl_boxes_v2" in a][0]
    assert "NULL::text AS status" in v2


def test_entity_comes_from_the_table_prefix_only_where_that_is_true():
    sql, _ = svc._plan(_full_schema(), with_txn=True)
    arms = _arms(sql)
    assert "'cdpl'::text AS company" in arms["cdpl_boxes_v2"]
    # The interunit tables' entity lives on the header's sites — guessing from
    # the table name would be wrong, so it stays null.
    assert "NULL::text AS company" in arms["interunit_transfer_boxes"]


def test_divergent_column_names_are_mapped_per_branch():
    sql, _ = svc._plan(_full_schema(), with_txn=True)
    arms = _arms(sql)
    assert "b.article_description::text AS item_description" in arms["cfpl_boxes_v2"]
    assert "b.article::text AS item_description" in arms["interunit_transfer_boxes"]
    assert "b.item_description::text AS item_description" in arms["jb_inward_boxes"]
    assert "b.lot_no::text AS lot_number" in arms["jb_inward_boxes"]
    assert "b.weight_kg::numeric AS net_weight" in arms["cfpl_cold_stocks"]


# ── tables held back on purpose ─────────────────────────────────────────────
def test_unverified_pre_v2_and_pending_tables_are_not_planned():
    """{cfpl|cdpl}_boxes has only `lot_number` evidenced; pending_transfer_stock
    is a product decision. Neither may appear until confirmed live."""
    keys = {s["key"] for s in svc._SPECS}
    assert "boxes_pre_v2" not in keys
    assert "pending_transfer_stock" not in keys
    schema = _full_schema() | {
        "cfpl_boxes": frozenset({"box_id", "transaction_no", "lot_number"}),
        "pending_transfer_stock": frozenset({"box_id", "transaction_no"}),
    }
    _sql, sources = svc._plan(schema, with_txn=True)
    assert "cfpl_boxes" not in sources
    assert "pending_transfer_stock" not in sources


# ── end to end, against a fake connection ───────────────────────────────────
class _Conn:
    """Answers the information_schema probe from `schema`, and the planned UNION
    from `rows`. `raise_on_union` simulates a live query failure."""

    def __init__(self, schema, rows=(), raise_on_union=None):
        self.schema = schema
        self.rows = list(rows)
        self.raise_on_union = raise_on_union
        self.union_sql = None

    async def fetch(self, sql, *args):
        if sql.lstrip().startswith("SELECT column_name"):
            return [{"column_name": c} for c in sorted(self.schema.get(args[0], ()))]
        self.union_sql = sql
        if self.raise_on_union:
            raise self.raise_on_union
        return self.rows


def _run(coro):
    return asyncio.run(coro)


def test_tx_bi_hit_reports_the_table_it_matched():
    row = {"_src": "cfpl_boxes_v2", "_prio": 1, "box_id": "91483060-1",
           "transaction_no": "TR-20260415124824", "item_description": "Cashew",
           "lot_number": "L1", "net_weight": 10.5, "gross_weight": 11.0,
           "count": 1, "status": None, "job_card_number": None, "company": "cfpl"}
    c = _Conn(_full_schema(), rows=[row])
    out = _run(svc.identify_box(c, '{"tx":"TR-20260415124824","bi":"91483060-1"}'))
    assert out["found"] is True
    assert out["table"] == "cfpl_boxes_v2"
    assert out["company"] == "cfpl"
    assert out["matched_by"] == "tx+bi"
    assert out["box"]["net_weight"] == 10.5
    assert "ambiguous" not in out


def test_two_matches_are_reported_ambiguous_not_silently_resolved():
    """A wrong box reported as found is worse than a not-found."""
    base = {"box_id": "91483060-1", "transaction_no": "TR-1", "item_description": "x",
            "lot_number": None, "net_weight": None, "gross_weight": None,
            "count": None, "status": None, "job_card_number": None, "company": None}
    rows = [dict(base, _src="cfpl_boxes_v2", _prio=1),
            dict(base, _src="cfpl_cold_stocks", _prio=3)]
    c = _Conn(_full_schema(), rows=rows)
    out = _run(svc.identify_box(c, '{"tx":"TR-1","bi":"91483060-1"}'))
    assert out["found"] is True
    assert out["table"] == "cfpl_boxes_v2"
    assert out["ambiguous"] is True
    assert out["also_in"] == ["cfpl_cold_stocks"]


def test_no_match_returns_not_found_with_the_scanned_id():
    c = _Conn(_full_schema(), rows=[])
    out = _run(svc.identify_box(c, '{"tx":"TR-1","bi":"nope"}'))
    assert out == {"found": False, "box_id": "nope"}


def test_bare_id_uses_the_box_id_pass():
    row = {"_src": "sfg_box", "_prio": 1, "box_id": "48-2", "transaction_no": None,
           "item_description": "FG", "lot_number": None, "net_weight": None,
           "gross_weight": None, "count": None, "status": "open",
           "job_card_number": "PLAN-1", "company": "cfpl"}
    c = _Conn(_full_schema(), rows=[row])
    out = _run(svc.identify_box(c, "48-2"))
    assert out["found"] is True
    assert out["matched_by"] == "carton_id"
    assert "$2" not in c.union_sql          # box-id pass binds one parameter


def test_unexpected_db_errors_propagate_instead_of_becoming_not_found():
    """The old code caught asyncpg.PostgresError, and DataError IS a subclass of
    it (verified: DataError -> PostgresError -> PostgresMessage), so a type
    mismatch or a permission failure looked exactly like a missing box. Now only
    the two recoverable schema errors are caught and everything else surfaces.

    Note the BIGINT carton_id hazard is neutralised upstream rather than here:
    every branch compares `col::text = $1`, so nothing ever binds a str to an
    int column and no DataError arises from that path in the first place."""
    c = _Conn(_full_schema(), raise_on_union=asyncpg.DataError("bad input"))
    with pytest.raises(asyncpg.DataError):
        _run(svc.identify_box(c, '{"tx":"TR-1","bi":"x"}'))


def test_schema_drift_is_recovered_and_the_plan_cache_cleared():
    """UndefinedColumn under a cached plan is the one recoverable case: it means
    the schema moved, so drop the cache and let the next scan re-plan."""
    c = _Conn(_full_schema(), raise_on_union=asyncpg.UndefinedColumnError("gone"))
    out = _run(svc.identify_box(c, '{"tx":"TR-1","bi":"x"}'))
    assert out == {"found": False, "box_id": "x"}
    assert svc._COLUMNS == {}


def test_column_probe_is_cached_across_scans():
    calls = []

    class _Counting(_Conn):
        async def fetch(self, sql, *args):
            if sql.lstrip().startswith("SELECT column_name"):
                calls.append(args[0])
            return await super().fetch(sql, *args)

    c = _Counting(_full_schema(), rows=[])
    _run(svc.identify_box(c, '{"tx":"TR-1","bi":"a"}'))
    first = len(calls)
    assert first > 0
    _run(svc.identify_box(c, '{"tx":"TR-1","bi":"b"}'))
    assert len(calls) == first, "information_schema was re-read on the second scan"


def test_absent_table_is_warned_once_not_on_every_scan(caplog):
    """A scan endpoint that reprints the same WARNING per request drowns out its
    own signal. The column probe is cached; the warning needs its own guard."""
    import logging
    schema = _full_schema()
    del schema["jb_inward_boxes"]
    c = _Conn(schema, rows=[])
    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        _run(svc.identify_box(c, '{"tx":"TR-1","bi":"a"}'))
        after_first = [r for r in caplog.records if "jb_inward_boxes" in r.getMessage()]
        _run(svc.identify_box(c, '{"tx":"TR-1","bi":"b"}'))
        after_second = [r for r in caplog.records if "jb_inward_boxes" in r.getMessage()]
    assert len(after_first) == 1, "the absent table should be reported"
    assert len(after_second) == 1, "and reported only once, not once per scan"


def test_schema_drift_reopens_warnings(caplog):
    """After a drift-triggered cache clear the next plan may drop different
    branches, so the suppressor must reset with the column cache."""
    c = _Conn(_full_schema(), raise_on_union=asyncpg.UndefinedColumnError("gone"))
    _run(svc.identify_box(c, '{"tx":"TR-1","bi":"x"}'))
    assert svc._COLUMNS == {}
    assert svc._WARNED == set()


# ── gaps the adversarial review found: each of these passed before, with the
# ── behaviour it names deleted.
def test_display_column_loss_degrades_to_null_without_dropping_the_branch():
    """`need` is identity-only on purpose. Requiring display columns meant a
    cosmetic rename silently killed a whole branch — the same silent miss the
    module exists to prevent, arriving by a different door."""
    schema = _full_schema()
    schema["interunit_transfer_boxes"] = frozenset({"box_id", "transaction_no"})
    sql, sources = svc._plan(schema, with_txn=True)
    assert "interunit_transfer_boxes" in sources, "identity intact — must still run"
    arm = _arms(sql)["interunit_transfer_boxes"]
    assert "NULL::text AS item_description" in arm
    assert "NULL::text AS lot_number" in arm


def test_exact_branch_membership_and_priority_order():
    """Pins WHICH tables are scanned and in what order. Without this, deleting a
    branch this rewrite added — or emitting a constant _prio — passes the suite."""
    import re as _re
    sql, sources = svc._plan(_full_schema(), with_txn=True)
    assert sources == [
        "cfpl_boxes_v2", "cdpl_boxes_v2",
        "cfpl_bulk_entry_boxes", "cdpl_bulk_entry_boxes",
        "cfpl_cold_stocks", "cdpl_cold_stocks",
        "cfpl_rtv_boxes", "cdpl_rtv_boxes",
        "interunit_transfer_boxes", "interunit_transfer_in_boxes",
        "cold_transfer_inboxes", "jb_inward_boxes", "po_box",
    ]
    prios = [int(m) for m in _re.findall(r"(\d+)::int AS _prio", sql)]
    assert prios == list(range(1, len(sources) + 1)), "priority must be distinct+ordered"
    # sfg_box joins only the box-id pass, and lands there too.
    _s2, src2 = svc._plan(_full_schema(), with_txn=False)
    assert "sfg_box" in src2 and len(src2) == len(sources) + 1


def test_join_table_drift_is_warned_not_silent(caplog):
    """rtv_boxes is the ONLY branch that can supply a CR-/RTV- transaction number
    (the box row has no transaction_no). A drifted rtv_header dropped BOTH rtv
    branches with zero log — breaking the guarantee in this module's docstring."""
    import logging
    schema = _full_schema()
    schema["cfpl_rtv_header"] = frozenset({"id"})        # rtv_id gone
    del schema["cdpl_rtv_header"]                        # table gone entirely
    c = _Conn(schema, rows=[])
    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        _run(svc.identify_box(c, '{"tx":"CR-20260101120000","bi":"a"}'))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("cfpl_rtv_header" in m for m in msgs), "drifted join must be reported"
    assert any("cdpl_rtv_header" in m for m in msgs), "absent join must be reported"
    _sql, sources = svc._plan(schema, with_txn=True)
    assert "cfpl_rtv_boxes" not in sources and "cdpl_rtv_boxes" not in sources


def test_partial_drift_warns_once_and_names_the_column(caplog):
    """The batch_number outage shape: table present, one needed column gone."""
    import logging
    schema = _full_schema()
    schema["jb_inward_boxes"] = frozenset({"box_id", "item_description"})  # no transaction_no
    c = _Conn(schema, rows=[])
    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        _run(svc.identify_box(c, '{"tx":"TR-1","bi":"a"}'))
        first = [r for r in caplog.records if "jb_inward_boxes" in r.getMessage()]
        _run(svc.identify_box(c, '{"tx":"TR-1","bi":"b"}'))
        second = [r for r in caplog.records if "jb_inward_boxes" in r.getMessage()]
    assert len(first) == 1 and "transaction_no" in first[0].getMessage()
    assert len(second) == 1, "must not reprint on every scan"


def test_tx_bi_miss_falls_back_to_box_id_only():
    """The fallback was entirely untested — deleting it passed the whole suite."""
    row = {"_src": "po_box", "_prio": 13, "box_id": "9-1", "transaction_no": "TR-9",
           "item_description": None, "lot_number": None, "net_weight": None,
           "gross_weight": None, "count": None, "status": None,
           "job_card_number": None, "company": None}

    class _TwoPass(_Conn):
        async def fetch(self, sql, *args):
            if sql.lstrip().startswith("SELECT column_name"):
                return await super().fetch(sql, *args)
            return [] if len(args) == 2 else [row]      # tx+bi misses, box-id hits

    out = _run(svc.identify_box(_TwoPass(_full_schema()), '{"tx":"TR-9","bi":"9-1"}'))
    assert out["found"] is True
    assert out["matched_by"] == "box_id_only"
    assert out["table"] == "po_box"


def test_sfg_description_falls_back_to_sfg_code():
    """The old alias tuple covered both; dropping the fallback returned a null
    article for older cartons, which the scan write path rejects."""
    sql, _ = svc._plan(_full_schema(), with_txn=False)
    arm = _arms(sql)["sfg_box"]
    assert "COALESCE(NULLIF(b.fg_sku_name, ''), b.sfg_code)::text AS item_description" in arm


def test_jb_inward_projects_gross_weight():
    sql, _ = svc._plan(_full_schema(), with_txn=True)
    assert "b.gross_weight::numeric AS gross_weight" in _arms(sql)["jb_inward_boxes"]


def test_prefix_hint_reaches_the_box_id_pass():
    """sfg_box appears ONLY in the box-id pass, so a PLAN-/MPG- hint that is only
    honoured by the transaction pass is unreachable dead code."""
    _sql, plain = svc._plan(_full_schema(), with_txn=False)
    _sql2, hinted = svc._plan(_full_schema(), with_txn=False, hint="sfg_box")
    assert hinted[0] == "sfg_box"
    assert plain[0] != "sfg_box"
    assert set(plain) == set(hinted), "a hint reorders, it never excludes"


def test_hint_is_actually_threaded_into_the_fallback_pass():
    """Testing _plan(hint=...) directly does NOT cover the wiring. Dropping the
    hint argument from _search_by_id's _plan call left every _plan-level test
    green — this asserts the SQL the fallback really issues."""
    seen = []

    class _Rec(_Conn):
        async def fetch(self, sql, *args):
            if sql.lstrip().startswith("SELECT column_name"):
                return await super().fetch(sql, *args)
            seen.append(sql)
            return []          # both passes miss; we only care about the plan

    out = _run(svc.identify_box(_Rec(_full_schema()),
                                '{"tx":"PLAN-20260101","bi":"48-2"}'))
    assert out["found"] is False
    assert len(seen) == 2, "expected the tx+bi pass then the box-id fallback"
    fallback = seen[1]
    arm = _arms(fallback)["sfg_box"]
    assert "1::int AS _prio" in arm, (
        "PLAN- must float sfg_box to the front of the fallback plan; "
        "the hint is not reaching _search_by_id")
