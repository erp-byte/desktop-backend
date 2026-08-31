"""Stage 4 — Save Output is the last step and completes the batch.

The Close Batch button is gone from the job card. POST /job-cards-v2/{id}/outputs
now takes `complete_batch`, and when it is set the handler runs the SAME
close_batch service the (still-present) /batches/{id}/close route calls.

WHAT THESE TESTS ARE ABOUT — the orchestration, not the closing itself.
close_batch's own behaviour (row lock, dispatch, downstream unlock, the output
UPSERT) is covered where it lives. What is new, and what breaks silently if it
regresses, is:

  * completion runs LAST — after byproducts / balance materials / additives / QC
    are persisted. Closing is the act that states what was made, so it must not
    stamp the batch before the batch's own accounting rows exist;
  * the balance gate SAVES BUT DOES NOT COMPLETE, rather than 4xx-ing. A refusal
    that rolled the transaction back would throw away everything the operator
    just typed, which is worse than an open batch;
  * `complete_batch` defaults FALSE, so the Android client — which posts the v1
    twin of this body and has no Complete step — is untouched;
  * a close that the service refuses must NOT look like a successful save.

Both services are patched: this file asserts what the handler ASKS them to do.

Run:  PYTHONPATH=. python -m pytest tests/services/test_output_completes_batch.py
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

JC = 74337017
BATCH = 22384101
URL = f"/api/v1/production/job-cards-v2/{JC}/outputs"

SESSION = {
    "user_id": 11,
    "phone": "9876511111",
    "full_name": "Asha Operator",
    "email": "asha@candorfoods.in",
    "entity": "cfpl",
    "role_id": 3,
    "role_name": "floor_manager",
    "is_admin": True,          # short-circuits require_permission, no DB call
    "role_ids": [3],
}


def _session(**over) -> dict:
    s = dict(SESSION)
    s.update(over)
    return s


class _Conn:
    """Answers the reads the outputs handler issues before the services run."""

    def __init__(self, *, open_batches=(BATCH,)):
        self.open_batches = list(open_batches)
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM job_card_batch_v2" in sql and "status = 'open'" in sql:
            return [{"batch_id": b} for b in self.open_batches]
        if "auth_role_permission" in sql:
            # check_permission only queries this for a NON-admin caller (admins
            # short-circuit). Granting unscoped means the non-admin tests reach
            # the handler at all — without it they 403 at the gate and can
            # never exercise the behaviour they are about.
            return [{"allowed_entities": None, "allowed_warehouses": None,
                     "allowed_floors": None}]
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM   job_card_batch_v2 WHERE batch_id" in sql:
            return {"status": "open", "job_card_id": JC}
        return None

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"

    def transaction(self):
        class _Tx:
            async def __aenter__(_s): return None
            async def __aexit__(_s, *exc): return False
        return _Tx()


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


@pytest.fixture
def spy(monkeypatch):
    """Patch the two write services and record the order they are called in.

    Returns a dict with `order` (call names in sequence) and `close_kwargs`.
    `close_result` is writable so a test can make the service refuse.
    """
    import app.modules.production.services.job_card_v2 as jc_svc
    import app.modules.production.services.job_card_batch_v2 as batch_svc
    import app.modules.production.services.jc_accounting_v2 as acct_svc

    state = {"order": [], "close_kwargs": None,
             "close_result": {"closed": True, "batch_id": BATCH}}

    async def _not_locked(conn, job_card_id):
        return None

    async def _record_output(conn, **kw):
        state["order"].append("record_output")
        return {"recorded": True, "output_qty_kg": kw.get("output_qty_kg")}

    async def _upsert_lines(conn, **kw):
        state["order"].append("upsert_consumption_lines")
        return {"rows": []}

    async def _save_byproducts(conn, **kw):
        state["order"].append("save_byproducts")
        return {"rows": []}

    async def _close_batch(conn, **kw):
        state["order"].append("close_batch")
        state["close_kwargs"] = kw
        return state["close_result"]

    monkeypatch.setattr(jc_svc, "assert_not_locked", _not_locked)
    monkeypatch.setattr(jc_svc, "record_output", _record_output)
    monkeypatch.setattr(jc_svc, "upsert_consumption_lines", _upsert_lines)
    monkeypatch.setattr(batch_svc, "close_batch", _close_batch)
    monkeypatch.setattr(acct_svc, "save_byproducts", _save_byproducts)
    return state


@pytest.fixture
def client_with(monkeypatch):
    def _make(conn, session=None):
        sess = session or SESSION
        import app.modules.auth.services.auth_service as auth_service

        async def _validate(_conn, _token):
            return sess

        monkeypatch.setattr(auth_service, "validate_session", _validate)
        app.state.db_pool = _Pool(conn)
        return TestClient(app, headers={"Authorization": "Bearer test-token"})
    return _make


def _body(**over) -> dict:
    b = {"output_qty_kg": 149.8, "rm_consumed_kg": 150.0}
    b.update(over)
    return b


# ── The default: nothing changes for existing callers ───────────────────────
def test_save_without_complete_batch_does_not_close(client_with, spy):
    """complete_batch defaults False. Android posts the v1 twin of this body and
    has no Complete step, so the default is what keeps it working."""
    res = client_with(_Conn()).post(URL, json=_body())
    assert res.status_code == 200, res.text
    assert "close_batch" not in spy["order"]
    assert res.json().get("completed") is None


# ── The new behaviour ───────────────────────────────────────────────────────
def test_complete_batch_closes_via_the_shared_service(client_with, spy):
    res = client_with(_Conn()).post(URL, json=_body(
        complete_batch=True, is_balanced=True, output_qty_units=1200,
        process_loss_kg=0.2, closure_remarks="shift A"))
    assert res.status_code == 200, res.text

    body = res.json()
    assert body["completed"] is True
    assert "close_batch" in spy["order"]

    kw = spy["close_kwargs"]
    assert kw["batch_id"] == BATCH
    assert kw["job_card_id"] == JC
    # close_batch is authoritative for produced_qty_kg and must receive the same
    # FG figure record_output was given, or the batch row and the output row
    # disagree about what was made.
    assert kw["produced_qty_kg"] == 149.8
    assert kw["rm_consumed_kg"] == 150.0
    assert kw["closure_remarks"] == "shift A"
    assert kw["closed_by"] == SESSION["full_name"]


def test_completion_runs_after_every_other_section_is_persisted(client_with, spy):
    """Closing states what was made, so the batch's own accounting rows must
    already exist when it runs. Closing at the record_output call site instead
    would stamp the batch before byproducts/balance/QC were written."""
    res = client_with(_Conn()).post(URL, json=_body(
        complete_batch=True, is_balanced=True,
        byproducts=[{"category": "offgrade", "qty_kg": 0.2, "uom": "KGS"}]))
    assert res.status_code == 200, res.text

    order = spy["order"]
    assert "close_batch" in order and "save_byproducts" in order
    assert order.index("close_batch") > order.index("save_byproducts"), order
    assert order.index("close_batch") > order.index("record_output"), order
    assert order[-1] == "close_batch", order


def test_operator_partial_dispatch_choice_reaches_close_batch(client_with, spy):
    """Removing the Close Batch button must not remove the DECISION it carried:
    omitting dispatch_qty_kg means the whole produced qty flows downstream."""
    res = client_with(_Conn()).post(URL, json=_body(
        complete_batch=True, is_balanced=True, dispatch_qty_kg=100.0))
    assert res.status_code == 200, res.text
    assert spy["close_kwargs"]["dispatch_qty_kg"] == 100.0


# ── The balance gate ────────────────────────────────────────────────────────
def test_unbalanced_batch_saves_but_does_not_complete(client_with, spy):
    """A 4xx here would roll the transaction back and discard everything the
    operator just typed. Saved, not completed, is the better failure."""
    res = client_with(_Conn()).post(URL, json=_body(
        complete_batch=True, is_balanced=False, balance_difference_qty=-149.8))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["completed"] is False
    assert body["completion_blocked"]["error"] == "unbalanced"
    assert body["completion_blocked"]["balance_difference_qty"] == -149.8
    # The save itself stands...
    assert "record_output" in spy["order"]
    # ...and the batch is left open.
    assert "close_batch" not in spy["order"]


def test_admin_can_override_an_unbalanced_completion(client_with, spy):
    res = client_with(_Conn()).post(URL, json=_body(
        complete_batch=True, is_balanced=False, allow_unbalanced=True))
    assert res.status_code == 200, res.text
    assert res.json()["completed"] is True
    assert spy["close_kwargs"]["allow_unbalanced"] is True


def test_a_non_admin_cannot_override_an_unbalanced_completion(client_with, spy):
    """allow_unbalanced is admin-only, matching /batches/{id}/close. A non-admin
    sending the flag is ignored, not obeyed."""
    conn = _Conn()
    res = client_with(conn, _session(is_admin=False)).post(URL, json=_body(
        complete_batch=True, is_balanced=False, allow_unbalanced=True))
    assert res.status_code in (200, 403), res.text
    if res.status_code == 200:
        assert res.json()["completed"] is False
        assert "close_batch" not in spy["order"]


def test_a_non_admins_override_flag_never_reaches_the_service(client_with, spy):
    """The handler's own gate is not the only consumer of allow_unbalanced —
    close_batch uses it to bypass ITS internal balance check too. So the flag
    must be downgraded on the way in, not merely ignored by the gate.

    This case deliberately sends is_balanced=True so the gate PASSES and
    execution actually reaches the service call; the previous non-admin test
    is blocked at the gate and so never exercises this line at all.
    """
    conn = _Conn()
    res = client_with(conn, _session(is_admin=False)).post(URL, json=_body(
        complete_batch=True, is_balanced=True, allow_unbalanced=True))
    assert res.status_code == 200, res.text
    assert res.json()["completed"] is True
    assert spy["close_kwargs"]["allow_unbalanced"] is False, (
        "a non-admin's allow_unbalanced must be downgraded before close_batch "
        "sees it, or the service bypasses its own balance check")


def test_unknown_balance_state_still_completes(client_with, spy):
    """is_balanced omitted means "the client did not compute it" — only an
    explicit False is a refusal. Otherwise every legacy caller that never sent
    the field would silently stop being able to complete."""
    res = client_with(_Conn()).post(URL, json=_body(complete_batch=True))
    assert res.status_code == 200, res.text
    assert res.json()["completed"] is True


# ── A refused close must not read as a successful save ──────────────────────
def test_a_service_level_close_refusal_is_surfaced_not_swallowed(client_with, spy):
    spy["close_result"] = {"error": "batch_not_open", "status": "closed"}
    res = client_with(_Conn()).post(URL, json=_body(
        complete_batch=True, is_balanced=True))
    assert res.status_code == 409, res.text
    # The app's exception handler FLATTENS HTTPException.detail onto the
    # envelope root (error / message / request_id / timestamp / details), so
    # the machine code is top-level, not nested under "detail".
    assert res.json()["error"] == "batch_not_open"


def test_batch_jc_mismatch_from_the_service_is_a_400(client_with, spy):
    spy["close_result"] = {"error": "batch_jc_mismatch"}
    res = client_with(_Conn()).post(URL, json=_body(
        complete_batch=True, is_balanced=True))
    assert res.status_code == 400, res.text


# ── Batch resolution still applies to completion ────────────────────────────
def test_completion_needs_an_unambiguous_batch(client_with, spy):
    """Two open batches: the handler already 400s before any write. Completion
    must not invent a batch to close."""
    res = client_with(_Conn(open_batches=(11, 22))).post(URL, json=_body(
        complete_batch=True, is_balanced=True))
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "ambiguous_open_batch"
    assert "close_batch" not in spy["order"]


def test_no_open_batch_cannot_be_completed(client_with, spy):
    res = client_with(_Conn(open_batches=())).post(URL, json=_body(
        complete_batch=True, is_balanced=True))
    assert res.status_code == 400, res.text
    assert res.json()["error"] == "no_open_batch"
    assert "close_batch" not in spy["order"]


# ═══════════════════════════════════════════════════════════════════════════
#  record_output must UPSERT, not append
# ═══════════════════════════════════════════════════════════════════════════
# The tests above monkeypatch record_output, so none of them can see its SQL --
# which is exactly how a real defect got through: saving output now issues TWO
# POSTs in one operator click (the save, then the completion once the accounting
# summary has established the authoritative is_balanced). Both carry an FG qty,
# so both reach record_output for the SAME (job_card_id, batch_id).
#
# Migration 092 added uq_jc_output_v2_live_per_batch -- UNIQUE (job_card_id,
# COALESCE(batch_id, 0)) WHERE deleted_at IS NULL. A bare INSERT therefore
# raises UniqueViolationError on the second call, and insert_with_pk_retry only
# swallows *_pkey violations, so it surfaced as a 500 and the batch never
# completed -- with the Close Batch button now deleted, leaving no way to close
# it at all.
#
# These assert on the STATEMENT, which is the only thing a fake connection can
# prove. They fail against a bare INSERT.
def _output_insert_sql() -> str:
    """The statement record_output hands the connection."""
    import asyncio

    captured = {}

    class _C:
        async def fetchrow(self, sql, *args):
            if "INSERT INTO job_card_output_v2" in sql:
                captured["sql"] = sql
                captured["args"] = args
                return {"output_id": 1, "job_card_id": JC, "batch_id": BATCH}
            # assert_not_locked's guard. Matched on the columns rather than the
            # FROM clause: the real statement spaces it "FROM   job_card_v2",
            # and a whitespace-sensitive match here silently returns None ->
            # job_card_not_found -> record_output bails before the INSERT, and
            # the test fails for a reason that has nothing to do with the SQL
            # it is meant to be asserting on.
            if "is_locked" in sql:
                return {"is_locked": False, "locked_reason": None,
                        "force_unlocked": False, "status": "in_progress"}
            if "output_kind" in sql:
                return {"job_card_id": JC, "output_kind": "SFG"}
            return None

        async def fetch(self, sql, *args):
            return []

        async def execute(self, sql, *args):
            return "UPDATE 1"

        # insert_with_pk_retry refuses to run outside a transaction (it opens a
        # SAVEPOINT so a PK-collision retry cannot commit partial state), so the
        # fake has to look like one.
        def is_in_transaction(self):
            return True

        def transaction(self):
            class _Tx:
                async def __aenter__(_s): return None
                async def __aexit__(_s, *exc): return False
            return _Tx()

    from app.modules.production.services import job_card_v2 as jc_svc

    async def _go():
        return await jc_svc.record_output(
            _C(), job_card_id=JC, rm_consumed_kg=150.0, output_qty_kg=149.8,
            batch_id=BATCH, recorded_by="Asha")

    asyncio.run(_go())
    assert "sql" in captured, "record_output never issued its INSERT"
    _LAST_INSERT.clear()
    _LAST_INSERT.update(captured)
    return captured["sql"]


# Same call, but the bound parameters rather than the statement. The SQL can be
# right and the value still wrong: `or 0` mangled it before it ever reached PG.
_LAST_INSERT: dict = {}


def _output_insert_args() -> tuple:
    _output_insert_sql()
    return _LAST_INSERT["args"]


def test_record_output_upserts_against_the_092_partial_index():
    sql = _output_insert_sql()
    assert "ON CONFLICT" in sql, (
        "record_output must UPSERT: uq_jc_output_v2_live_per_batch makes a "
        "second save for the same (job_card_id, batch_id) a UniqueViolation, "
        "which insert_with_pk_retry re-raises as a 500")
    assert "COALESCE(batch_id, 0)" in sql, "must infer the 092 index's expression"
    assert "DO UPDATE" in sql, "DO NOTHING would silently discard the save"


def test_record_output_conflict_target_repeats_the_partial_predicate():
    """Postgres will not match a partial index without its predicate -- omit it
    and the statement fails at runtime with 'no unique or exclusion constraint
    matching the ON CONFLICT specification'."""
    sql = _output_insert_sql()
    head = sql[sql.index("ON CONFLICT"):sql.index("DO UPDATE")]
    assert "deleted_at IS NULL" in head, head


def test_record_output_upsert_preserves_free_text_it_was_not_given():
    """The completion POST carries no notes, and it must not wipe the note the
    operator's own save (or the accounting record) already wrote."""
    sql = _output_insert_sql()
    body = sql[sql.index("DO UPDATE"):]
    for col in ("notes", "process_loss_remark"):
        assert f"COALESCE(EXCLUDED.{col}" in body, (
            f"{col} must be COALESCEd, or a second write nulls it")
    # ...while the numbers the completion POST DOES send are overwritten: for
    # those, and only those, the later save really is the truth. process_loss_kg
    # was in this list and did not belong -- the completion POST omits it, which
    # is precisely the situation the COALESCE above exists to handle. See
    # test_record_output_upsert_preserves_process_loss_it_was_not_given.
    for col in ("output_qty_kg", "rm_consumed_kg"):
        assert f"{col}       = EXCLUDED.{col}" in body or \
               f"{col}      = EXCLUDED.{col}" in body or \
               f"{col}     = EXCLUDED.{col}" in body or \
               f"{col} = EXCLUDED.{col}" in body, col


# ═══════════════════════════════════════════════════════════════════════════
#  process_loss_kg survives the completion POST
# ═══════════════════════════════════════════════════════════════════════════
# The Process Loss blanking bug. One SAVE BATCH click fires three requests and
# only the first carries the operator's figure:
#
#   1. POST /outputs                     process_loss_kg = 12.5   -> stored
#   2. PUT  /accounting/summary          process_loss_qty = 12.5
#   3. POST /outputs {complete_batch}    process_loss_kg ABSENT   -> None
#
# Request 3 does send output_qty_kg, so has_output_payload is true and
# record_output runs a second time; close_batch then runs in the same
# transaction. Both bound `process_loss_kg or 0` into an unguarded
# `= EXCLUDED.process_loss_kg`, so the absent field arrived as 0 and overwrote
# the 12.5 request 1 had stored seconds earlier. The operator's number was gone
# before the page finished reloading.
#
# `notes` and `process_loss_remark`, sitting in the same DO UPDATE, are COALESCEd
# for exactly this reason. process_loss_kg was left out of that fix.
#
# Why COALESCE on the PARAMETER and not on EXCLUDED: None means "the caller said
# nothing, keep what is stored", but 0.0 means "the operator typed zero" and must
# still be written. `or 0` could not tell those apart. The VALUES clause has to
# coerce NULL -> 0 to satisfy the NOT NULL that migration 026 added, so
# EXCLUDED.process_loss_kg is never NULL and cannot carry the distinction.
def test_record_output_binds_none_rather_than_zero_when_unset():
    """`or 0` turned "the caller said nothing" into a real 0 before the value
    ever reached Postgres -- no SQL-side guard can recover it after that."""
    args = _output_insert_args()
    assert args[11] is None, (
        f"record_output bound {args[11]!r} for $12 (process_loss_kg) when the "
        "caller passed nothing; None has to survive into the statement for "
        "COALESCE to keep the stored value")


def test_record_output_upsert_preserves_process_loss_it_was_not_given():
    sql = _output_insert_sql()
    body = sql[sql.index("DO UPDATE"):]
    assert "COALESCE($12, job_card_output_v2.process_loss_kg)" in body, (
        "the completion POST carries no process_loss_kg, so a bare "
        "= EXCLUDED.process_loss_kg zeroes what the operator's own save stored "
        "seconds earlier -- this is the Process Loss blanking bug")


def test_record_output_insert_still_satisfies_the_not_null_column():
    """026 made the column NOT NULL DEFAULT 0. A genuine first INSERT with no
    process_loss must still write 0, not NULL."""
    sql = _output_insert_sql()
    values = sql[sql.index("VALUES"):sql.index("ON CONFLICT")]
    assert "COALESCE($12, 0)" in values, values


def _close_batch_output_sql() -> str:
    """close_batch's own job_card_output_v2 UPSERT -- the SECOND writer to run
    on the completion POST, inside the same transaction."""
    import inspect

    from app.modules.production.services import job_card_batch_v2 as bsvc
    src = inspect.getsource(bsvc.close_batch)
    i = src.index("INSERT INTO job_card_output_v2")
    return src[i:src.index("RETURNING *", i)]


def test_close_batch_output_upsert_preserves_process_loss_it_was_not_given():
    """Guarding only record_output would leave the value zeroed: close_batch
    writes the same row afterwards, in the same transaction, and wins."""
    sql = _close_batch_output_sql()
    body = sql[sql.index("DO UPDATE"):]
    assert "COALESCE($12, job_card_output_v2.process_loss_kg)" in body, body


def test_close_batch_output_insert_still_satisfies_the_not_null_column():
    sql = _close_batch_output_sql()
    values = sql[sql.index("VALUES"):sql.index("ON CONFLICT")]
    assert "COALESCE($12, 0)" in values, values


def test_completion_post_really_does_omit_process_loss(client_with, spy):
    """The premise the guards rest on. If this ever starts arriving populated the
    client changed; the guards stay correct either way, but this records why
    they exist."""
    client = client_with(_Conn())
    r = client.post(URL, json=_body(complete_batch=True, is_balanced=True))
    assert r.status_code == 200, r.text
    assert spy["close_kwargs"]["process_loss_kg"] is None
