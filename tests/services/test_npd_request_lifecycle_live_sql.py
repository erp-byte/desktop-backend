"""LIVE, READ-ONLY check of the NPD request-lifecycle SQL against real data.

The session is put in default_transaction_read_only, so nothing here can write. Expected
values are never pinned: each test recomputes the rule in plain Python from the raw tables
and compares it with what the SQL says, for every NPD / TRIAL request in the database.

Skipped unless NPD_TEST_DATABASE_URL is set:
    NPD_TEST_DATABASE_URL=postgresql://.../warehouse_db \
    PYTHONPATH=. .venv/Scripts/python -m pytest tests/services/test_npd_request_lifecycle_live_sql.py
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from decimal import Decimal

import asyncpg
import pytest

from app.modules.sample.services import dispatch_reminder_service as drs
from app.modules.sample.services import npd_dev_service as nds
from app.modules.sample.services import requisition_service as rs

URL = (os.environ.get("NPD_TEST_DATABASE_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
pytestmark = pytest.mark.skipif(not URL, reason="NPD_TEST_DATABASE_URL not set")
TOL = Decimal("0.0005")


def _run(fn):
    async def go():
        conn = await asyncpg.connect(URL, statement_cache_size=0)
        try:
            await conn.execute("SET default_transaction_read_only = on")
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


async def _oracle(conn) -> dict[int, str]:
    """The display bucket of every NPD/TRIAL request, from the raw tables, in Python."""
    reqs = await conn.fetch(
        "SELECT * FROM sample_requisitions WHERE deleted_at IS NULL AND sample_type IN ('NPD','TRIAL')")
    want = {r["requisition_id"]: r["q"] for r in await conn.fetch(
        "SELECT requisition_id, SUM(quantity) AS q FROM sample_requisition_npd_targets GROUP BY 1")}
    cards = await conn.fetch("SELECT * FROM npd_dev_job_cards WHERE source_requisition_id IS NOT NULL")
    arts = defaultdict(list)
    for a in await conn.fetch("SELECT * FROM npd_dev_job_card_articles"):
        arts[a["dev_jc_id"]].append(a)
    disp_rows = await conn.fetch("SELECT * FROM npd_dev_dispatch")
    by_art, by_card = defaultdict(Decimal), defaultdict(Decimal)
    for d in disp_rows:
        by_card[d["dev_jc_id"]] += d["qty"]
        if d["article_id"] is not None:
            by_art[d["article_id"]] += d["qty"]
    card_by_id = {c["id"]: c for c in cards}

    ledger = defaultdict(lambda: [Decimal(0), Decimal(0), set()])     # any, kg, units
    for d in disp_rows:
        c = card_by_id.get(d["dev_jc_id"])
        if not c:
            continue
        unit = (d["uom"] or c["output_uom"] or c["uom"] or "kg").lower()
        e = ledger[c["source_requisition_id"]]
        e[0] += d["qty"]
        e[1] += d["qty"] if unit == "kg" else 0
        e[2].add(unit)

    def card_shipped(c) -> bool:
        if c["status"] != "CLOSED":
            return False
        a = arts.get(c["id"])
        if a:
            made = [x for x in a if (x["output_qty"] or 0) > 0]
            return bool(made) and all(by_art[x["article_id"]] >= x["output_qty"] - TOL for x in made)
        out = c["output_qty"] or 0
        shipped = max(by_card[c["id"]], c["dispatch_qty"] or 0)
        return out > 0 and shipped >= out - TOL

    all_out: dict[int, bool] = {}
    for c in cards:
        if c["status"] == "CANCELLED":
            continue
        rid = c["source_requisition_id"]
        all_out[rid] = all_out.get(rid, True) and card_shipped(c)

    out = {}
    for r in reqs:
        s = r["status"]
        if r.get("bh_signoff_state") == "PENDING":
            b = "BH_PENDING"
        elif s == "ON_HOLD":
            b = "HOLD"
        elif s in ("CANCELLED", "BH_REJECTED"):
            b = "CANCELLED"
        elif s in ("DRAFT", "SUBMITTED"):
            b = "PENDING"
        elif s in ("GATE_PASS_ISSUED", "CLOSED"):
            b = "DISPATCHED"
        else:
            q_any, q_kg, units = ledger.get(r["id"], (0, 0, set()))
            asked = want.get(r["id"], r["quantity"])
            by_qty = len(units) <= 1 and asked is not None and asked > 0 and q_kg >= asked - Decimal("0.001")
            if q_any > 0 and (by_qty or all_out.get(r["id"], False)):
                b = "DISPATCHED"
            elif q_any > 0:
                b = "PARTIAL"
            else:
                b = "IN_PROCESS"
        out[r["id"]] = b
    return out


def test_queue_buckets_match_the_rule_for_every_request():
    async def go(conn):
        expected = await _oracle(conn)
        rows = await rs.list_requisitions(conn, sample_types=["NPD", "TRIAL"], limit=200)
        got = {r["id"]: r["display_status"] for r in rows}
        assert set(got) == set(expected)
        diff = {k: (got[k], expected[k]) for k in got if got[k] != expected[k]}
        assert not diff, diff
    _run(go)


def test_the_single_request_bucket_agrees_with_the_queue():
    """close_if_fully_dispatched decides from _bucket_for; the queue shows display_status.
    If the two ever disagreed a request could close while the queue still said Partial."""
    async def go(conn):
        rows = await rs.list_requisitions(conn, sample_types=["NPD", "TRIAL"], limit=200)
        for r in rows:
            one = await rs._bucket_for(conn, r["id"])
            assert one["display_status"] == r["display_status"], r["request_id"]
    _run(go)


def test_the_chase_never_includes_a_request_with_a_closed_job_card():
    async def go(conn):
        today = drs.ist_today()
        b = await drs.due_buckets(conn, today)
        chased = {r["id"] for r in b["overdue"] + b["due_tomorrow"]}
        closed = {r["id"] for r in await conn.fetch(
            """SELECT DISTINCT sr.id FROM sample_requisitions sr
                 JOIN npd_dev_job_cards jc
                   ON (jc.source_requisition_id = sr.id OR jc.id = sr.linked_dev_jc_id)
                WHERE jc.status = 'CLOSED'""")}
        assert not chased & closed
        # …and still chases every open, dated request whose card is not closed.
        should = {r["id"] for r in await conn.fetch(
            f"""SELECT id, expected_dispatch_date FROM sample_requisitions
                 WHERE deleted_at IS NULL AND expected_dispatch_date IS NOT NULL
                   AND status = ANY($1::text[])
                   AND (expected_dispatch_date < $2::date OR expected_dispatch_date = $2::date + 1)""",
            list(drs.OPEN_STATUSES), today)} - closed
        assert chased == should
    _run(go)


def test_a_linked_card_shows_its_request_number():
    async def go(conn):
        jc = await conn.fetchrow(
            """SELECT jc.id, sr.request_id FROM npd_dev_job_cards jc
                 JOIN sample_requisitions sr ON sr.id = jc.source_requisition_id
                ORDER BY jc.created_at DESC LIMIT 1""")
        card = await nds.get_dev_job_card(conn, jc["id"])
        assert card["source_request_id"] == jc["request_id"]
    _run(go)
