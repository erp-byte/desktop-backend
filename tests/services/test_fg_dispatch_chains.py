"""FG dispatch for an article carded in several partial chains.

Before: the packaging card was picked with ORDER BY step_number DESC LIMIT 1, so
with two chains only one packaging card's batches were ever offered, and a batch
of the other chain was refused as batch_not_found — that chain's finished goods
could not be dispatched at all. And "emailed N people" was recorded even when
the mail server never accepted the message (email_sent ignored _send's False).
"""
from __future__ import annotations

import pytest

from app.modules.production.services import fg_dispatch_service as fgd

LINE = 5101
CARD_A = {"job_card_id": 11, "job_card_number": "PLAN-1-S3", "plan_id": 7, "fg_sku_name": "Roasted Almonds 200g",
          "customer_name": "Acme", "factory": "A-185", "floor": "FG store", "entity": "cfpl",
          "status": "completed", "uom": "kg"}
CARD_B = {**CARD_A, "job_card_id": 21, "job_card_number": "PLAN-1-S3-B2", "status": "in_progress"}
BATCHES = {
    11: [{"batch_id": 101, "batch_number": 1, "batch_date": None, "status": "closed",
          "produced_qty_kg": 100, "fg_actual_kg": 100, "fg_actual_units": 500, "out_kg": 100, "out_units": 500}],
    21: [{"batch_id": 201, "batch_number": 1, "batch_date": None, "status": "closed",
          "produced_qty_kg": 50, "fg_actual_kg": 50, "fg_actual_units": 250, "out_kg": 50, "out_units": 250}],
}


class _Conn:
    def __init__(self, cards=(CARD_A, CARD_B), emails=("billing@x.in",)):
        self.cards, self.emails = list(cards), list(emails)
        self.logged = None

    async def fetch(self, sql, *args):
        if "output_kind = 'FG'" in sql and "FROM job_card_v2 j" in sql:
            return self.cards
        if "FROM job_card_batch_v2 b" in sql:
            return BATCHES.get(args[0], [])
        if "auth_user" in sql:
            return [{"email": e} for e in self.emails]
        return []

    async def fetchval(self, sql, *args):
        if "INSERT INTO fg_dispatch_log_v2" in sql:
            self.logged = args
            return args[0]
        return None

    def is_in_transaction(self):
        return True

    def transaction(self):
        class _Tx:
            async def __aenter__(_s): return None
            async def __aexit__(_s, *exc): return False
        return _Tx()


@pytest.mark.asyncio
async def test_dispatch_info_offers_every_chains_batches():
    info = await fgd.get_line_dispatch_info(_Conn(), LINE)
    assert info["exists"] is True
    assert [(b["batch_id"], b["job_card_number"]) for b in info["batches"]] == [
        (101, "PLAN-1-S3"), (201, "PLAN-1-S3-B2")]
    assert [c["job_card_id"] for c in info["packaging_cards"]] == [11, 21]
    # Not every packaging card is finished yet.
    assert info["packaging_completed"] is False


@pytest.mark.asyncio
async def test_one_chain_reads_as_before():
    info = await fgd.get_line_dispatch_info(_Conn(cards=[CARD_A]), LINE)
    assert info["job_card_id"] == 11 and info["packaging_completed"] is True
    assert [b["batch_id"] for b in info["batches"]] == [101]


@pytest.mark.asyncio
async def test_the_second_chains_batch_can_be_dispatched(monkeypatch):
    monkeypatch.setattr(fgd.mail_service, "_send", lambda *a, **k: True)
    conn = _Conn()
    out = await fgd.dispatch_fg(conn, LINE, batch_id=201, dispatched_by="Ravi K")
    assert out["dispatched"] is True
    # Recorded against chain B's own packaging card and batch.
    assert conn.logged[3] == 21 and conn.logged[4] == "PLAN-1-S3-B2" and conn.logged[5] == 201
    assert "PLAN-1-S3-B2" in out["subject"]


@pytest.mark.asyncio
async def test_a_batch_on_no_packaging_card_is_still_refused():
    out = await fgd.dispatch_fg(_Conn(), LINE, batch_id=999)
    assert out["error"] == "batch_not_found"


@pytest.mark.asyncio
async def test_email_sent_is_what_the_mail_server_said(monkeypatch):
    monkeypatch.setattr(fgd.mail_service, "_send", lambda *a, **k: False)   # SMTP off / failed
    conn = _Conn()
    out = await fgd.dispatch_fg(conn, LINE, batch_id=101)
    assert out["email_sent"] is False
    assert conn.logged[22] is False


@pytest.mark.asyncio
async def test_email_sent_when_accepted(monkeypatch):
    monkeypatch.setattr(fgd.mail_service, "_send", lambda *a, **k: True)
    out = await fgd.dispatch_fg(_Conn(), LINE, batch_id=101)
    assert out["email_sent"] is True
