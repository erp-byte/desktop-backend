"""The job card's combined outpass must mark its items as out.

The "Combined outpass — all articles" button (and "Download full outpass" on a
single-product card) used to only print. Samples left on paper while every article still
read as remaining, the R&D sample stock was never issued and the request never closed:
93 of the 100 closed cards had no dispatch recorded on 2026-11-06. The button now calls
dispatch_all_remaining first, which records each balance as an ordinary partial out.

No DB: the connection below keeps an in-memory dispatch ledger and rolls it back when the
outer transaction fails, the way Postgres would.

Run:  PYTHONPATH=. python -m pytest tests/services/test_npd_dispatch_all.py
"""
from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace

import asyncpg
import pytest
from fastapi import HTTPException

from app.modules.sample.services import npd_dev_service as nds
from app.modules.sample.services import requisition_service as rs
from app.modules.sample.services import sample_mail_service

USER = SimpleNamespace(user_id=237, role_name="npd_team", is_admin=False, full_name="NPD")
JC_ID = 20738983


class _Conn:
    """Enough of a connection for dispatch_dev_sample / dispatch_all_remaining."""

    def __init__(self, *, jc: dict, articles: list[dict], ledger: list[dict] | None = None):
        self.jc, self.articles = jc, articles
        self.ledger = list(ledger or [])
        self.card_updates = 0
        self._depth = 0
        self._snapshots: list[tuple[list, int]] = []

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self_inner):
                conn._snapshots.append((list(conn.ledger), conn.card_updates))
                conn._depth += 1

            async def __aexit__(self_inner, exc_type, *a):
                conn._depth -= 1
                ledger, updates = conn._snapshots.pop()
                if exc_type is not None:          # roll this (sub)transaction back
                    conn.ledger, conn.card_updates = ledger, updates
                return False
        return _T()

    def _sent(self, article_id=None):
        rows = [d for d in self.ledger if article_id is None or d["article_id"] == article_id]
        return sum(d["qty"] for d in rows)

    async def fetchrow(self, query, *args):
        if "FROM npd_dev_job_cards WHERE id = $1 FOR UPDATE" in query:
            return dict(self.jc)
        if "FROM npd_dev_job_card_articles WHERE article_id = $1 AND dev_jc_id = $2" in query:
            return next((dict(a) for a in self.articles if a["article_id"] == args[0]), None)
        raise AssertionError(f"unexpected fetchrow: {query[:70]}")

    async def fetch(self, query, *args):
        if "FROM npd_dev_job_card_articles a" in query:
            return [{"article_id": a["article_id"], "output_qty": a["output_qty"] or 0,
                     "sent": self._sent(a["article_id"])}
                    for a in sorted(self.articles, key=lambda a: a["line_order"])]
        raise AssertionError(f"unexpected fetch: {query[:70]}")

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM npd_dev_job_card_articles WHERE dev_jc_id" in query:
            return 1 if self.articles else None
        if "COALESCE(SUM(qty), 0) FROM npd_dev_dispatch WHERE dev_jc_id = $1 AND article_id = $2" in query:
            return self._sent(args[1])
        if "COALESCE(SUM(qty), 0) FROM npd_dev_dispatch WHERE dev_jc_id = $1" in query:
            return self._sent()
        if "COALESCE(MAX(seq), 0) + 1" in query:
            return max((d["seq"] for d in self.ledger), default=0) + 1
        if "information_schema.columns" in query:
            return 1
        raise AssertionError(f"unexpected fetchval: {query[:70]}")

    async def execute(self, query, *args):
        if "INSERT INTO npd_dev_dispatch" in query:
            if any(d["dispatch_id"] == args[0] for d in self.ledger):
                raise asyncpg.UniqueViolationError("duplicate key npd_dev_dispatch_pkey")
            has_article = "article_id" in query
            self.ledger.append({"dispatch_id": args[0], "seq": args[3] if has_article else args[2],
                                "article_id": args[2] if has_article else None,
                                "qty": args[4] if has_article else args[3]})
        elif "UPDATE npd_dev_job_cards" in query:
            self.card_updates += 1
        return "OK"


@pytest.fixture
def world(monkeypatch):
    """Stub the goods issue, the request close, the reload and the mail; record them."""
    w = SimpleNamespace(issues=[], closes=[], mails=[], fail_on=None)
    ids = itertools.count(90000001)

    async def fake_issue(conn, **kw):
        if w.fail_on and kw["sku_name"] == w.fail_on:
            raise HTTPException(409, detail={"error": "insufficient_stock"})
        w.issues.append((kw["batch_id"], kw["qty_kg"], kw["uom"]))
        return {"mat_doc_id": f"MD{len(w.issues)}"}

    async def fake_close(conn, req_id, *, user, remarks):
        w.closes.append((req_id, remarks))
        return True

    async def fake_get(conn, jc_id):
        return {"id": jc_id}

    async def fake_mail(conn, **kw):
        w.mails.append(kw)

    monkeypatch.setattr(nds.inv, "issue_named_batch", fake_issue)
    monkeypatch.setattr(rs, "close_if_fully_dispatched", fake_close)
    monkeypatch.setattr(nds, "get_dev_job_card", fake_get)
    monkeypatch.setattr(sample_mail_service, "notify_dev_dispatch_email", fake_mail)
    monkeypatch.setattr(nds, "new_short_time_id", lambda: next(ids))
    return w


def _card(**over):
    jc = {"id": JC_ID, "status": "CLOSED", "source_requisition_id": 186, "uom": "kg",
          "fg_sample_batch_id": "FG-CARD", "output_qty": 1.0, "dispatched_at": None,
          "dispatch_qty": None, "fg_sku_name": "Milk Chocolate coated dates",
          "title": "Milk Chocolate coated dates"}
    jc.update(over)
    return jc


def _art(aid, name, out, line, uom="kg"):
    return {"article_id": aid, "dev_jc_id": JC_ID, "name": name, "output_qty": out,
            "output_uom": uom, "fg_sample_batch_id": f"FG-{aid}", "line_order": line}


ARTS = [_art(11, "Dates 70%", 0.6, 1), _art(12, "Dates 55%", 0.4, 2, uom="pcs")]


def _all(conn, recipient="Courier"):
    return asyncio.run(nds.dispatch_all_remaining(conn, JC_ID, recipient=recipient, user=USER))


def test_every_article_is_recorded_as_out(world):
    conn = _Conn(jc=_card(), articles=ARTS)
    out = _all(conn)
    assert [(d["article_id"], d["qty"]) for d in conn.ledger] == [(11, 0.6), (12, 0.4)]
    assert world.issues == [("FG-11", 0.6, "kg"), ("FG-12", 0.4, "pcs")]  # stock leaves R&D
    assert out["dispatch_ids"] == [d["dispatch_id"] for d in conn.ledger]


def test_only_what_is_left_goes_out(world):
    """A part already dispatched is not sent (or printed) again."""
    conn = _Conn(jc=_card(), articles=ARTS + [_art(13, "Dates 40%", 0.5, 3)],
                 ledger=[{"dispatch_id": 1, "seq": 1, "article_id": 11, "qty": 0.6},
                         {"dispatch_id": 2, "seq": 2, "article_id": 13, "qty": 0.2}])
    out = _all(conn)
    new = [d for d in conn.ledger if d["dispatch_id"] in out["dispatch_ids"]]
    assert [(d["article_id"], round(d["qty"], 3), d["seq"]) for d in new] == [(12, 0.4, 3), (13, 0.3, 4)]


def test_an_article_with_no_output_is_skipped(world):
    conn = _Conn(jc=_card(), articles=[_art(11, "Dates 70%", 0.6, 1), _art(12, "Dropped", 0, 2)])
    _all(conn)
    assert [d["article_id"] for d in conn.ledger] == [11]


def test_nothing_left_is_refused_and_nothing_is_sent(world):
    conn = _Conn(jc=_card(), articles=ARTS,
                 ledger=[{"dispatch_id": 1, "seq": 1, "article_id": 11, "qty": 0.6},
                         {"dispatch_id": 2, "seq": 2, "article_id": 12, "qty": 0.4}])
    with pytest.raises(HTTPException) as e:
        _all(conn)
    assert e.value.status_code == 409 and e.value.detail["error"] == "nothing_to_dispatch"
    assert world.issues == [] and world.mails == []


def test_one_failing_article_records_none_of_them(world):
    """All or nothing: an outpass must never be printed for half a dispatch."""
    world.fail_on = "Dates 55%"
    conn = _Conn(jc=_card(), articles=ARTS)
    with pytest.raises(HTTPException):
        _all(conn)
    assert conn.ledger == []
    assert world.mails == [] and world.closes == []


def test_one_mail_and_one_request_check_for_the_whole_lot(world):
    conn = _Conn(jc=_card(), articles=ARTS)
    out = _all(conn)
    assert len(world.mails) == 1
    parts = world.mails[0]["parts"]
    assert [p["dispatch_id"] for p in parts] == out["dispatch_ids"]
    assert [p["name"] for p in parts] == ["Dates 70%", "Dates 55%"]
    assert world.closes == [(186, "Closed automatically: everything has been dispatched "
                                  f"(development job card {JC_ID}, outpass {JC_ID}-1+2)")]


def test_a_standalone_card_has_no_request_to_close(world):
    conn = _Conn(jc=_card(source_requisition_id=None), articles=ARTS)
    _all(conn)
    assert world.closes == []


def test_a_card_that_is_not_closed_cannot_dispatch(world):
    conn = _Conn(jc=_card(status="IN_DEVELOPMENT"), articles=ARTS)
    with pytest.raises(HTTPException) as e:
        _all(conn)
    assert e.value.status_code == 409 and e.value.detail["error"] == "wrong_status"


def test_a_single_product_card_sends_its_whole_balance(world):
    """The old "Download full outpass" button: one card-level part, mirrored on the card."""
    conn = _Conn(jc=_card(output_qty=2.5), articles=[])
    out = _all(conn)
    assert [(d["article_id"], d["qty"]) for d in conn.ledger] == [(None, 2.5)]
    assert world.issues == [("FG-CARD", 2.5, "kg")]
    assert conn.card_updates == 1
    assert len(out["dispatch_ids"]) == 1
    assert "parts" not in world.mails[0]          # one part → the ordinary single mail


def test_a_single_product_card_issues_in_its_output_unit(world):
    """Promote received the FG batch in output_uom. 3 closed cards on 2026-11-06 measure
    output in gm on a kg card: their issue must say 360 gm, not 360 kg."""
    conn = _Conn(jc=_card(output_qty=360, uom="kg", output_uom="gm"), articles=[])
    _all(conn)
    assert world.issues == [("FG-CARD", 360, "gm")]


def test_a_legacy_single_shot_dispatch_counts_as_sent(world):
    """Dispatched under the old 046 model (mirror set, no ledger row): nothing is left."""
    conn = _Conn(jc=_card(output_qty=2.5, dispatched_at="2026-07-01", dispatch_qty=2.5), articles=[])
    with pytest.raises(HTTPException) as e:
        _all(conn)
    assert e.value.detail["error"] == "nothing_to_dispatch"


def test_a_repeated_id_waits_for_the_clock_then_retries(world, monkeypatch):
    """Ids are epoch-ms and dispatch-all mints them back to back: the second part can
    get the first part's id. It must wait a moment and take a fresh one, not fail."""
    minted = iter([7, 7, 8])
    naps: list[float] = []

    async def fake_sleep(s):
        naps.append(s)

    monkeypatch.setattr(nds, "new_short_time_id", lambda: next(minted))
    monkeypatch.setattr(nds.asyncio, "sleep", fake_sleep)
    conn = _Conn(jc=_card(), articles=ARTS)
    out = _all(conn)
    assert out["dispatch_ids"] == [7, 8]
    assert naps == [nds.PK_RETRY_DELAY_S]


def test_a_single_part_dispatch_is_unchanged(world):
    conn = _Conn(jc=_card(), articles=ARTS)
    asyncio.run(nds.dispatch_dev_sample(conn, JC_ID, recipient="Apollo", qty=0.25,
                                        user=USER, article_id=11))
    assert [(d["article_id"], d["qty"], d["seq"]) for d in conn.ledger] == [(11, 0.25, 1)]
    assert len(world.mails) == 1 and "parts" not in world.mails[0]
    assert world.mails[0]["seq"] == 1 and world.mails[0]["qty"] == 0.25


# --- the mail ---------------------------------------------------------------------------

def test_the_combined_mail_links_one_outpass_for_every_part():
    jc = {"id": JC_ID, "fg_sku_name": "X"}
    parts = [{"dispatch_id": 501, "seq": 1, "qty": 0.6, "uom": "kg", "name": "Dates 70%"},
             {"dispatch_id": 502, "seq": 2, "qty": 0.4, "uom": "pcs", "name": "Dates 55%"}]
    html = sample_mail_service._dispatch_html(
        jc, dispatch_id=501, seq=1, qty=0.6, uom="kg", recipient="Courier", actor_name="N",
        dc_url="https://app.test/dc?dispatch=501,502", parts=parts)
    assert f"{JC_ID}-1+2" in html
    assert "0.6 kg + 0.4 pcs" in html            # per unit, never summed across them
    assert "Dates 70%" in html and "Dates 55%" in html


@pytest.mark.parametrize("qtys, want", [
    ([1200.5, 34.067], "1234.567 kg"),           # :g printed 1234.57
    ([1234567.5], "1234567.5 kg"),               # :g printed 1.23457e+06
    ([0.1, 0.2], "0.3 kg"),                      # float noise rounded away
    ([2.0], "2 kg"),
])
def test_the_mail_total_keeps_every_digit_of_the_ledger(qtys, want):
    parts = [{"qty": q, "uom": "kg"} for q in qtys]
    assert sample_mail_service._qty_by_uom(parts) == want
