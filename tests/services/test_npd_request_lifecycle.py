"""An NPD request has to finish: it closes when its sample has gone out, a job card built
without the Develop button can still be tied to its request, the overdue chase stops once
NPD has closed the job card, and a request cannot be raised already overdue.

Before this, nothing in the app moved a request past BH_APPROVED. The 34 closed requests
on 2026-10-20 were all closed by one manual bulk close on 2026-09-07; every request since
sat at "In process" with the overdue reminder chasing it daily, even after its sample was
dispatched (40794270: 164 reminders after its last dispatch). And "Dispatched" compared
the ledger against target #1's quantity only, so a 9-article request read as complete
after its first 0.449 kg of 2.299 kg went out.

No DB here: connections are stand-ins that record SQL. The SQL itself is checked against
real data, read-only, by test_npd_request_lifecycle_live_sql.py.

Run:  PYTHONPATH=. python -m pytest tests/services/test_npd_request_lifecycle.py
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.sample.services import dispatch_reminder_service as drs
from app.modules.sample.services import npd_dev_service as nds
from app.modules.sample.services import requisition_service as rs

USER = SimpleNamespace(user_id=237, role_name="npd_team", is_admin=False, full_name="NPD")
TODAY = date(2026, 10, 20)
LEDGER_COLS = ("dispatch_id", "dev_jc_id", "qty", "uom", "article_id")


def _probe_rows(cols=LEDGER_COLS, targets=True):
    out = [{"table_name": "npd_dev_dispatch", "column_name": c} for c in cols]
    if targets:
        out.append({"table_name": "sample_requisition_npd_targets", "column_name": "quantity"})
    return out


class _ListConn:
    def __init__(self, *, cols=LEDGER_COLS, targets=True):
        self.cols, self.targets = cols, targets
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        if "information_schema.columns" in query:
            return _probe_rows(self.cols, self.targets)
        return []


def _list_sql(**kw):
    conn = _ListConn(**kw)
    asyncio.run(rs.list_requisitions(conn))
    return next(q for q in conn.queries if "FROM sample_requisitions" in q)


# --- what "Dispatched" means ----------------------------------------------------------

def test_dispatched_is_measured_against_every_target_article():
    """sr.quantity mirrors target #1 only. The total asked for is the sum of the targets."""
    sql = _list_sql()
    assert "FROM sample_requisition_npd_targets" in sql
    assert "COALESCE(want.qty, sr.quantity)" in sql


def test_shipping_every_developed_article_also_counts():
    """NPD often adds or drops variants on the card (11 of 35 linked cards differ from
    their request). When every article the card produced has gone out, the request is
    done even if the kg differ from what was asked."""
    sql = _list_sql()
    assert "outs.all_out" in sql
    assert "FROM npd_dev_job_card_articles" in sql


def test_without_the_targets_table_the_header_quantity_still_works():
    """080 is hand-applied: a missing targets table must not 500 the queue."""
    sql = _list_sql(targets=False)
    assert "sample_requisition_npd_targets" not in sql
    assert "AS display_status" in sql


def test_without_per_article_dispatch_the_article_rule_is_left_out():
    """083 (npd_dev_dispatch.article_id) is hand-applied too."""
    sql = _list_sql(cols=("dispatch_id", "dev_jc_id", "qty", "uom"))
    assert "npd_dev_job_card_articles" not in sql
    assert "AS display_status" in sql


# --- closing a request automatically ---------------------------------------------------

class _CloseConn:
    def __init__(self, *, status="BH_APPROVED", bucket="DISPATCHED", updated=1):
        self.status, self.bucket, self.updated = status, bucket, updated
        self.executed: list[tuple[str, tuple]] = []
        self.bucket_sql = ""

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetch(self, query, *args):
        if "information_schema.columns" in query:
            return _probe_rows()
        raise AssertionError(f"unexpected fetch: {query[:60]}")

    async def fetchrow(self, query, *args):
        if "AS display_status" in query:
            self.bucket_sql = query
            return {"status": self.status, "display_status": self.bucket}
        raise AssertionError(f"unexpected fetchrow: {query[:60]}")

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if query.lstrip().startswith("UPDATE sample_requisitions"):
            return f"UPDATE {self.updated}"
        return "INSERT 0 1"


def _closing_updates(conn):
    return [(q, a) for q, a in conn.executed
            if "UPDATE sample_requisitions" in q and "'CLOSED'" in q]


def test_a_fully_dispatched_open_request_is_closed():
    conn = _CloseConn()
    closed = asyncio.run(rs.close_if_fully_dispatched(conn, 186, user=USER, remarks="done"))
    assert closed is True
    assert len(_closing_updates(conn)) == 1
    audits = [a for q, a in conn.executed if "INSERT INTO sample_audit_log" in q]
    assert audits and audits[0][0] == 186 and audits[0][1] == "STATUS_CHANGE"
    assert '"CLOSED"' in audits[0][3]


def test_the_bucket_is_computed_for_that_one_request():
    conn = _CloseConn()
    asyncio.run(rs.close_if_fully_dispatched(conn, 186, user=USER, remarks="done"))
    assert "sr.id = $1" in conn.bucket_sql


@pytest.mark.parametrize("bucket", ["PARTIAL", "IN_PROCESS", "HOLD", "PENDING", "CANCELLED"])
def test_a_request_that_is_not_fully_out_stays_open(bucket):
    conn = _CloseConn(bucket=bucket)
    closed = asyncio.run(rs.close_if_fully_dispatched(conn, 186, user=USER, remarks="done"))
    assert closed is False
    assert _closing_updates(conn) == []


@pytest.mark.parametrize("status", ["CLOSED", "GATE_PASS_ISSUED"])
def test_an_already_finished_request_is_left_alone(status):
    conn = _CloseConn(status=status)
    assert asyncio.run(rs.close_if_fully_dispatched(conn, 186, user=USER, remarks="x")) is False
    assert _closing_updates(conn) == []


def test_the_close_is_guarded_against_a_concurrent_status_change():
    """Someone cancels between the read and the write: the UPDATE matches nothing, so
    no 'closed' audit row may be written for a close that did not happen."""
    conn = _CloseConn(updated=0)
    assert asyncio.run(rs.close_if_fully_dispatched(conn, 186, user=USER, remarks="x")) is False
    assert not any("INSERT INTO sample_audit_log" in q for q, _ in conn.executed)
    q, args = _closing_updates(conn)[0]
    assert "status = ANY" in q


# --- the dispatch triggers it ----------------------------------------------------------

class _DispatchConn:
    """Just enough of dispatch_dev_sample's reads for one per-article part."""

    def __init__(self, jc: dict, article: dict):
        self.jc, self.article = jc, article
        self.executed: list[str] = []

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetchrow(self, query, *args):
        if "FROM npd_dev_job_cards WHERE id = $1 FOR UPDATE" in query:
            return dict(self.jc)
        if "FROM npd_dev_job_card_articles WHERE article_id" in query:
            return dict(self.article)
        raise AssertionError(f"unexpected fetchrow: {query[:70]}")

    async def fetchval(self, query, *args):
        if "COALESCE(SUM(qty), 0)" in query:
            return 0
        if "information_schema.columns" in query:
            return 1
        if "COALESCE(MAX(seq), 0) + 1" in query:
            return 1
        raise AssertionError(f"unexpected fetchval: {query[:70]}")

    async def execute(self, query, *args):
        self.executed.append(query)
        return "INSERT 0 1"


def _run_dispatch(monkeypatch, *, source_requisition_id):
    closes: list[int] = []

    async def fake_close(conn, req_id, *, user, remarks):
        closes.append(req_id)
        return True

    async def fake_issue(conn, **kw):
        return {"mat_doc_id": "MATDOC-1"}

    async def fake_get(conn, jc_id):
        return {"id": jc_id}

    async def fake_mail(*a, **kw):
        return None

    from app.modules.sample.services import sample_mail_service
    monkeypatch.setattr(rs, "close_if_fully_dispatched", fake_close)
    monkeypatch.setattr(nds.inv, "issue_named_batch", fake_issue)
    monkeypatch.setattr(nds, "get_dev_job_card", fake_get)
    monkeypatch.setattr(sample_mail_service, "notify_dev_dispatch_email", fake_mail)
    jc = {"id": 16096969, "status": "CLOSED", "source_requisition_id": source_requisition_id,
          "uom": "kg", "fg_sample_batch_id": "B", "output_qty": 6, "dispatched_at": None,
          "dispatch_qty": None, "fg_sku_name": "RS Almond", "title": "RS Almond"}
    art = {"article_id": 16096989, "dev_jc_id": 16096969, "name": "RS Almond",
           "fg_sample_batch_id": "FGSMP-1", "output_qty": 1.5, "output_uom": "kg"}
    conn = _DispatchConn(jc, art)
    asyncio.run(nds.dispatch_dev_sample(conn, 16096969, recipient="Apollo", qty=1.5,
                                        user=USER, article_id=16096989))
    return closes


def test_a_dispatch_checks_whether_its_request_is_now_complete(monkeypatch):
    assert _run_dispatch(monkeypatch, source_requisition_id=186) == [186]


def test_a_standalone_card_dispatch_has_no_request_to_close(monkeypatch):
    assert _run_dispatch(monkeypatch, source_requisition_id=None) == []


# --- linking a job card to its request afterwards --------------------------------------

class _LinkConn:
    def __init__(self, *, jc: dict, req: dict | None):
        self.jc, self.req = jc, req
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        class _T:
            async def __aenter__(self_inner): return None
            async def __aexit__(self_inner, *a): return False
        return _T()

    async def fetchval(self, query, *args):
        if "npd_authorized_users" in query:
            return 0
        raise AssertionError(f"unexpected fetchval: {query[:70]}")

    async def fetchrow(self, query, *args):
        if "FROM npd_dev_job_cards WHERE id = $1" in query:
            return dict(self.jc)
        if "FROM sample_requisitions" in query:
            return dict(self.req) if self.req else None
        raise AssertionError(f"unexpected fetchrow: {query[:70]}")

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


def _jc(**over):
    base = {"id": 34302860, "status": "CLOSED", "source_requisition_id": None,
            "promoted_bom_id": 1700, "confirmed_dispatch_date": date(2026, 9, 10),
            "closed_at": None}
    base.update(over)
    return base


def _rq(**over):
    base = {"id": 178, "request_id": 33461896, "sample_type": "NPD", "status": "BH_APPROVED",
            "deleted_at": None, "linked_dev_jc_id": None}
    base.update(over)
    return base


def _link(monkeypatch, conn, requisition_id=178):
    closes: list[int] = []

    async def fake_close(c, req_id, *, user, remarks):
        closes.append(req_id)
        return False

    async def fake_get(c, jc_id):
        return {"id": jc_id}

    monkeypatch.setattr(rs, "close_if_fully_dispatched", fake_close)
    monkeypatch.setattr(nds, "get_dev_job_card", fake_get)
    asyncio.run(nds.link_to_requisition(conn, 34302860, requisition_id=requisition_id, user=USER))
    return closes


def test_linking_sets_the_card_source_and_back_links_the_request(monkeypatch):
    conn = _LinkConn(jc=_jc(), req=_rq())
    closes = _link(monkeypatch, conn)
    card = [a for q, a in conn.executed if "UPDATE npd_dev_job_cards" in q and "source_requisition_id" in q]
    assert card and card[0][:2] == (34302860, 178)
    back = [q for q, _ in conn.executed if "UPDATE sample_requisitions" in q]
    assert back and "COALESCE(linked_dev_jc_id" in back[0]
    assert closes == [178], "a card already dispatched may complete the request on link"


def test_linking_a_closed_card_hands_its_result_to_the_request(monkeypatch):
    """What closing the card would have done had it been linked at the time: the
    confirmed dispatch date and the promoted BOM (never overwriting one already set)."""
    conn = _LinkConn(jc=_jc(), req=_rq())
    _link(monkeypatch, conn)
    q = next(q for q, _ in conn.executed if "UPDATE sample_requisitions" in q)
    assert "confirmed_dispatch_date" in q and "COALESCE(base_bom_id" in q


def test_linking_is_audited_on_the_request(monkeypatch):
    conn = _LinkConn(jc=_jc(), req=_rq())
    _link(monkeypatch, conn)
    audits = [a for q, a in conn.executed if "INSERT INTO sample_audit_log" in q]
    assert audits and audits[0][0] == 178 and "34302860" in audits[0][6]


@pytest.mark.parametrize("jc_over,req_over,code", [
    ({"source_requisition_id": 99}, {}, 409),          # already linked
    ({"status": "CANCELLED"}, {}, 409),                  # dead card
    ({}, {"sample_type": "BASIS_RM"}, 422),              # not an NPD request
    ({}, {"status": "CLOSED"}, 409),                     # finished request
    ({}, {"status": "CANCELLED"}, 409),
    ({}, {"deleted_at": "2026-10-01"}, 404),
])
def test_linking_refuses_what_it_should(monkeypatch, jc_over, req_over, code):
    conn = _LinkConn(jc=_jc(**jc_over), req=_rq(**req_over))
    with pytest.raises(HTTPException) as e:
        _link(monkeypatch, conn)
    assert e.value.status_code == code
    assert not any(q.lstrip().startswith("UPDATE") for q, _ in conn.executed)


def test_linking_to_a_missing_request_is_404(monkeypatch):
    conn = _LinkConn(jc=_jc(), req=None)
    with pytest.raises(HTTPException) as e:
        _link(monkeypatch, conn)
    assert e.value.status_code == 404


# --- the overdue chase -----------------------------------------------------------------

class _ScanConn:
    def __init__(self):
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []


def test_the_chase_skips_a_request_whose_job_card_is_closed():
    """Once NPD closes the card the sample exists and its dispatch date is confirmed —
    chasing the business head daily about BD's original date is noise."""
    conn = _ScanConn()
    asyncio.run(drs.due_buckets(conn, TODAY))
    sql = conn.queries[0]
    assert "NOT EXISTS" in sql and "FROM npd_dev_job_cards" in sql
    assert "jc.status = 'CLOSED'" in sql
    assert "jc.source_requisition_id = sr.id" in sql and "jc.id = sr.linked_dev_jc_id" in sql


# --- no request raised already overdue -------------------------------------------------

def test_a_new_request_cannot_be_dated_in_the_past(monkeypatch):
    monkeypatch.setattr(rs, "_ist_today", lambda: TODAY)

    class _NoDb:
        def __getattr__(self, name):
            raise AssertionError(f"touched the DB ({name}) before validating the date")

    payload = {"sample_type": "NPD", "warehouse": "W202",
               "targets": [{"name": "Date Bites", "pcs": 20, "weight_per_piece": 0.021}],
               "expected_dispatch_date": TODAY - timedelta(days=1)}
    with pytest.raises(HTTPException) as e:
        asyncio.run(rs.create_requisition(_NoDb(), payload=payload, user=USER))
    assert e.value.status_code == 422
    assert e.value.detail["error"] == "dispatch_date_in_past"


def test_an_iso_string_date_is_checked_too(monkeypatch):
    monkeypatch.setattr(rs, "_ist_today", lambda: TODAY)
    with pytest.raises(HTTPException):
        rs._assert_dispatch_date_not_past("2026-08-27")


def test_today_is_allowed(monkeypatch):
    monkeypatch.setattr(rs, "_ist_today", lambda: TODAY)
    rs._assert_dispatch_date_not_past(TODAY)
    rs._assert_dispatch_date_not_past(None)


def test_an_edit_that_leaves_an_old_date_alone_is_not_refused(monkeypatch):
    """The edit modal sends every field back. A request raised in September carries a
    September date; editing its description must not trip over it."""
    monkeypatch.setattr(rs, "_ist_today", lambda: TODAY)
    rs._assert_dispatch_date_not_past(date(2026, 9, 4), current=date(2026, 9, 4))


def test_an_edit_that_moves_the_date_into_the_past_is_refused(monkeypatch):
    monkeypatch.setattr(rs, "_ist_today", lambda: TODAY)

    class _Conn:
        async def fetchrow(self, query, *args):
            return {"id": 42, "status": "SUBMITTED", "sample_type": "NPD",
                    "expected_dispatch_date": date(2026, 10, 25)}

    with pytest.raises(HTTPException) as e:
        asyncio.run(rs.update_requisition(
            _Conn(), 42, payload={"expected_dispatch_date": date(2026, 10, 1)}, user=USER))
    assert e.value.detail["error"] == "dispatch_date_in_past"


# --- the route ---------------------------------------------------------------------------

def test_the_link_route_passes_the_request_through(monkeypatch):
    from app.modules.sample import router as sample_router
    from app.modules.sample import schemas

    route = next(r for r in sample_router.router.routes
                 if getattr(r, "path", "") == "/api/v1/sample/npd-dev-job-cards/{dev_jc_id}/link-request")
    assert route.methods == {"POST"}

    seen = {}

    async def fake_link(conn, dev_jc_id, *, requisition_id, user):
        seen.update(dev_jc_id=dev_jc_id, requisition_id=requisition_id, user=user)
        return {"id": dev_jc_id}

    monkeypatch.setattr(nds, "link_to_requisition", fake_link)

    class _Acquire:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        db_pool=SimpleNamespace(acquire=lambda: _Acquire()))))
    out = asyncio.run(sample_router.link_dev_job_card_request(
        request, 34302860, schemas.DevJobCardLinkRequest(requisition_id=178), USER))
    assert out == {"id": 34302860}
    assert seen == {"dev_jc_id": 34302860, "requisition_id": 178, "user": USER}
