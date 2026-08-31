"""Minting box ids for inward boxes that were never labelled.

`box_id` is not written at inward time — it is stamped when the approver prints
the label. So an unprinted transaction has real stock whose rows carry no
scannable identity, and `interunit_tools.py:687` filters those rows out of every
downstream read with `AND COALESCE(b.box_id,'') <> ''`. This service mints the
missing ids in bulk, matching `inward_tools.upsert_box`'s format exactly, and
attaches the boxes to a job card.

It WRITES to a live warehouse table that has no CREATE TABLE in this repo, so the
properties these tests pin are mostly about refusing to do the wrong thing:

  * verify the columns or raise — never guess, never partially write;
  * never overwrite an id the approver already printed, even under a race;
  * never mint an id that already exists, because box_id is in no unique key and
    a duplicate makes the box unresolvable by scan — the exact harm this fixes;
  * mint in one transaction, attach only after it commits.

Run:  PYTHONPATH=. python -m pytest tests/services/test_box_id_assign.py
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.production.services import box_id_assign_service as svc
from app.modules.production.services import box_identify_service as ident_svc
from app.modules.production.services.box_id_assign_service import BoxIdAssignError

V2_COLS = frozenset({"box_id", "box_number", "transaction_no", "line_number",
                     "article_description", "lot_number", "net_weight"})
PRE_V2_COLS = frozenset({"box_id", "transaction_no", "lot_number", "id"})
# Neither a box_number nor an id: the only key left is ctid.
BARE_COLS = frozenset({"box_id", "transaction_no", "lot_number"})


class _Conn:
    """Serves information_schema from `schema`, unlabelled rows from `unlabelled`,
    existing ids from `taken`. Records every UPDATE issued."""

    def __init__(self, schema, unlabelled=None, taken=(), has_rows=(),
                 update_result="UPDATE 1"):
        self.schema = schema
        self.unlabelled = unlabelled or {}      # table -> [ {box_number, line_number} ]
        self.taken = list(taken)
        self.has_rows = set(has_rows)           # tables that hold rows for the txn
        self.update_result = update_result
        self.updates = []                       # (sql, args)
        self.selects = []
        self.began = 0

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT column_name"):
            return [{"column_name": c} for c in sorted(self.schema.get(args[0], ()))]
        if s.startswith("SELECT box_id FROM"):
            return [{"box_id": t} for t in self.taken]
        if " AS _key," in s:
            table = s.split(" FROM ")[1].split(" ")[0]
            rows = [dict(r) for r in self.unlabelled.get(table, [])]
            for i, r in enumerate(rows, start=1):
                r.setdefault("_key", i)
                r.setdefault("box_number", None)
                r.setdefault("line_number", None)
            if len(args) > 1:                   # narrowed by box_number
                rows = [r for r in rows if r["box_number"] == args[1]]
            if " LIMIT " in s:
                rows = rows[:int(s.rsplit(" LIMIT ", 1)[1])]
            self.selects.append(s)
            return rows
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT 1 FROM"):
            return 1 if s.split(" FROM ")[1].split(" ")[0] in self.has_rows else None
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    async def execute(self, sql, *args):
        self.updates.append((" ".join(sql.split()), args))
        return self.update_result

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self_inner):
                conn.began += 1
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False
        return _T()


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear():
    ident_svc._COLUMNS.clear()
    yield
    ident_svc._COLUMNS.clear()


def _v2_schema():
    return {"cfpl_boxes_v2": V2_COLS, "cfpl_boxes": PRE_V2_COLS}


# ── refusing to guess ────────────────────────────────────────────────────────
def test_unverified_columns_raise_and_write_nothing():
    """{cfpl|cdpl}_boxes has no CREATE TABLE in this repo and only lot_number was
    ever evidenced. If the column map does not hold, this must refuse — a partial
    write into a live warehouse table is the worst possible outcome."""
    schema = {"cfpl_boxes": frozenset({"lot_number"})}   # no box_id at all
    c = _Conn(schema, has_rows={"cfpl_boxes"})
    with pytest.raises(BoxIdAssignError) as e:
        _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert "missing" in str(e.value)
    assert c.updates == []


def test_absent_table_raises_and_writes_nothing():
    c = _Conn({}, has_rows=set())
    with pytest.raises(BoxIdAssignError):
        _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert c.updates == []


@pytest.mark.parametrize("company", ["", None, "ACME", "cfpl'; DROP TABLE x --"])
def test_company_is_whitelisted(company):
    """The prefix is interpolated into SQL, so it can never come from raw input.

    Asserting only `raises` is too weak: with the whitelist gone the call still
    raises, just later and because 'acme_boxes_v2' is not found. Pin the REASON,
    and that no query was ever built from the raw value."""
    seen = []

    class _Watch(_Conn):
        async def fetch(self, sql, *args):
            seen.append((sql, args))
            return await super().fetch(sql, *args)

    c = _Watch(_v2_schema())
    with pytest.raises(BoxIdAssignError) as e:
        _run(svc.assign_box_ids(c, company=company, transaction_no="TR-1"))
    assert "CFPL or CDPL" in str(e.value), "must fail on the whitelist, not later"
    assert seen == [], "nothing may be queried before the company is validated"
    assert c.updates == []


def test_lowercase_and_padded_company_is_accepted():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]})
    out = _run(svc.assign_box_ids(c, company="  cfpl  ", transaction_no="TR-1"))
    assert out["table"] == "cfpl_boxes_v2"


def test_blank_transaction_is_refused():
    c = _Conn(_v2_schema())
    with pytest.raises(BoxIdAssignError):
        _run(svc.assign_box_ids(c, company="CFPL", transaction_no="   "))


# ── table selection ─────────────────────────────────────────────────────────
def test_prefers_v2_then_falls_back_to_pre_v2():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2", "cfpl_boxes"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 3}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert out["table"] == "cfpl_boxes_v2"

    ident_svc._COLUMNS.clear()
    c2 = _Conn(_v2_schema(), has_rows={"cfpl_boxes"},      # v2 has no rows for it
               unlabelled={"cfpl_boxes": [{}]})
    out2 = _run(svc.assign_box_ids(c2, company="CFPL", transaction_no="TR-1"))
    assert out2["table"] == "cfpl_boxes"


# ── the id format is copied from upsert_box, not invented ───────────────────
def test_v2_id_carries_the_line_number():
    """`{base}-{line}-{box}`. The line component stops two same-name articles
    printed in the same millisecond minting the same id — the base is shared."""
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 7, "line_number": 2}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    box_id = out["minted"][0]["box_id"]
    base, line, box = box_id.split("-")
    assert (len(base), line, box) == (8, "2", "7")


def test_no_box_number_falls_back_to_the_counter_format():
    """The pre-v2 table has no box_number, so upsert_box's format cannot apply.
    generate_box_ids' `{base}-{i}` counter is the legacy answer for exactly this."""
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes"},
              unlabelled={"cfpl_boxes": [{}, {}, {}]})     # three bare rows
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    ids = [m["box_id"] for m in out["minted"]]
    base = ids[0].split("-")[0]
    assert len(base) == 8
    assert ids == [f"{base}-1", f"{base}-2", f"{base}-3"]


def test_limit_labels_one_arbitrary_box_deterministically():
    """Rows with no box_number are fungible, so "a" box means an arbitrary one —
    but arbitrary must be reproducible, or the same call twice does different
    things and a support question has no answer."""
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes"},
              unlabelled={"cfpl_boxes": [{}, {}, {}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1", limit=1))
    assert len(out["minted"]) == 1
    assert len(c.updates) == 1
    assert out["minted"][0]["row_key"] == "1", "lowest key first, not random"
    assert "ORDER BY _key" in c.selects[0]


def test_box_number_filter_is_refused_when_the_table_has_none():
    """Silently ignoring the filter would label the wrong carton."""
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes"},
              unlabelled={"cfpl_boxes": [{}]})
    with pytest.raises(BoxIdAssignError) as e:
        _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1", box_number=2))
    assert "no box_number column" in str(e.value)
    assert c.updates == []


def test_row_without_a_surrogate_id_is_keyed_on_ctid():
    """An unkeyed UPDATE would stamp every row in the transaction with one id."""
    c = _Conn({"cfpl_boxes": BARE_COLS}, has_rows={"cfpl_boxes"},
              unlabelled={"cfpl_boxes": [{}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert out["key"] == "ctid::text"
    sql, _a = c.updates[0]
    assert "ctid = $3::tid" in sql


def test_surrogate_id_is_preferred_over_ctid():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes"}, unlabelled={"cfpl_boxes": [{}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert out["key"] == "id"
    assert "id = $3" in c.updates[0][0]


@pytest.mark.parametrize("bad", [0, -1])
def test_limit_must_be_positive(bad):
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes"})
    with pytest.raises(BoxIdAssignError):
        _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1", limit=bad))


# ── never clobber a printed id ──────────────────────────────────────────────
def test_update_repeats_the_null_guard_in_its_own_where():
    """The guard must be on the UPDATE, not only the SELECT: if the approver
    prints between the two, their id has to win."""
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]})
    _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    sql, _args = c.updates[0]
    assert "UPDATE cfpl_boxes_v2 SET box_id" in sql
    assert "(box_id IS NULL OR box_id = '')" in sql


def test_losing_the_print_race_is_skipped_not_reported_as_minted():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]},
              update_result="UPDATE 0")          # someone printed first
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert out["minted"] == []
    assert out["skipped"] == 1


# ── never mint a duplicate ──────────────────────────────────────────────────
def test_collision_with_an_existing_id_is_reminted(monkeypatch):
    """box_id is in no unique key and the 8-digit base repeats about every 27.7h.
    A duplicate would make the box unresolvable by scan — the exact harm this
    service exists to remove.

    The clock is pinned: deriving the expected base from a second time.time()
    call lets a millisecond of drift make the clash string miss, and the test
    then passes without the re-mint ever running."""
    monkeypatch.setattr(svc.time, "time", lambda: 1_760_000_099.999)
    base = str(int(1_760_000_099.999 * 1000))[-8:]
    clash = f"{base}-1-1"
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]},
              taken=[clash])
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    got = out["minted"][0]["box_id"]
    assert got != clash, "must not re-use an id that already exists"
    assert got.endswith("-1-1"), "only the base moves; the suffix is identity"


def test_exhausted_remints_raise_rather_than_write_a_duplicate(monkeypatch):
    """Refusing beats writing an id that makes the box unscannable."""
    monkeypatch.setattr(svc.time, "time", lambda: 1_760_000_099.999)
    base = int(str(int(1_760_000_099.999 * 1000))[-8:])
    taken = [f"{str(base + i).zfill(8)[-8:]}-1-1" for i in range(svc._MAX_REMINT + 1)]
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]},
              taken=taken)
    with pytest.raises(BoxIdAssignError) as e:
        _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert "duplicate" in str(e.value)


def test_two_boxes_in_one_call_get_distinct_ids():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [
                  {"box_number": 1, "line_number": 5},
                  {"box_number": 2, "line_number": 5}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    ids = [m["box_id"] for m in out["minted"]]
    assert len(ids) == 2 and len(set(ids)) == 2


# ── scoping ─────────────────────────────────────────────────────────────────
def test_box_number_narrows_to_one_carton():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [
                  {"box_number": 1, "line_number": 1},
                  {"box_number": 2, "line_number": 1}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1",
                                  box_number=2))
    assert [m["box_number"] for m in out["minted"]] == [2]
    assert len(c.updates) == 1


def test_nothing_unlabelled_is_a_no_op():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": []})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert out["minted"] == [] and out["skipped"] == 0
    assert c.updates == []


def test_minting_runs_inside_a_transaction():
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]})
    _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert c.began == 1, "a partial mint must not be able to commit"


# ── job-card attach ─────────────────────────────────────────────────────────
def test_attach_calls_scan_box_after_the_commit(monkeypatch):
    """scan_box resolves through identify_box and must NOT run inside a
    transaction (its own docstring), so the attach happens post-commit."""
    calls = []

    async def _fake_scan_box(conn, *, job_card_id, code, **kw):
        calls.append((job_card_id, code, conn.began))
        return {"ok": True}

    import app.modules.production.services.box_scan_service as bss
    monkeypatch.setattr(bss, "scan_box", _fake_scan_box)

    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1",
                                  job_card_id=99))
    assert len(calls) == 1
    jc, code, _ = calls[0]
    assert jc == 99 and code == out["minted"][0]["box_id"]
    assert out["attached"] == [{"box_id": code, "ok": True}]


def test_attach_is_skipped_when_no_job_card_given(monkeypatch):
    called = []

    async def _fake_scan_box(conn, **kw):
        called.append(kw)
        return {"ok": True}

    import app.modules.production.services.box_scan_service as bss
    monkeypatch.setattr(bss, "scan_box", _fake_scan_box)
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert called == [] and out["attached"] == []


def test_attach_surfaces_a_scan_error_instead_of_claiming_success(monkeypatch):
    async def _fake_scan_box(conn, **kw):
        return {"error": "duplicate_box", "box_id": kw["code"]}

    import app.modules.production.services.box_scan_service as bss
    monkeypatch.setattr(bss, "scan_box", _fake_scan_box)
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [{"box_number": 1, "line_number": 1}]})
    out = _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1",
                                  job_card_id=1))
    assert out["attached"][0]["error"] == "duplicate_box"


def test_v2_labels_come_out_in_carton_order():
    """Where box numbers exist the pick is still arbitrary-but-deterministic, and
    carton order is the useful determinism — labels print in the order someone
    would physically stack them, not in row-key order."""
    c = _Conn(_v2_schema(), has_rows={"cfpl_boxes_v2"},
              unlabelled={"cfpl_boxes_v2": [
                  {"_key": 90, "box_number": 1, "line_number": 1},
                  {"_key": 10, "box_number": 2, "line_number": 1}]})
    _run(svc.assign_box_ids(c, company="CFPL", transaction_no="TR-1"))
    assert "ORDER BY box_number, _key" in c.selects[0]
