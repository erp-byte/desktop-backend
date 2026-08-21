"""An NPD reviewer's non-NPD chatter must not draw an ACCEPT/HOLD reply.

Being an NPD reviewer is a property of the PERSON: _resolve_reviewer only asks
"is this phone an npd_team/admin user?". So every unrelated thing a reviewer typed
fell through to the command parser and drew

    Reply  ACCEPT <request#>  or  HOLD <request#>  — or tap the Accept / Hold
    button on the request.

while the very same webhook body was relayed to the maintenance bot
(router.py -> forward_maintenance, unconditional), which answered with its module
menu. The reviewer got two replies to one "hi" — the same
two-systems-answer-one-message bug already fixed for NON-reviewers in the
unattributed branch (4e00810), never applied to recognised ones.

Reproduces the reported thread: "hi", then the "Maintenance" list reply.

Run:  PYTHONPATH=. python -m pytest tests/services/test_wa_reviewer_small_talk.py
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules.sample.services import whatsapp_service as ws

REVIEWER_PHONE = "919876543210"
REVIEW_WAMID = "wamid.review.card"


class _Conn:
    """Stand-in connection. The only query the fixed path runs is the
    wa_review_message lookup that asks 'does this reply quote an NPD review card?'."""

    def __init__(self, known_review_wamids=()):
        self.known = set(known_review_wamids)
        self.queries: list[str] = []

    async def fetchval(self, query, *args):
        self.queries.append(" ".join(query.split()))
        if "wa_review_message" in query:
            # These wamids are REVIEW cards, so the 086 business-head lookup — which
            # is scoped to kind='BH_SIGNOFF' — misses them. Without this the stub
            # answers every wa_review_message query alike and the tap is read as a
            # business-head approval instead of review traffic.
            if "BH_SIGNOFF" in query:
                return None
            return 1 if args and args[0] in self.known else None
        return None

    async def fetchrow(self, query, *args):
        self.queries.append(" ".join(query.split()))
        return None

    async def execute(self, query, *args):
        self.queries.append(" ".join(query.split()))


@pytest.fixture
def wa(monkeypatch):
    """Reviewer recognised, no pending prompts, maintenance bot configured,
    outbound sends captured instead of hitting Meta."""
    sent: list[tuple[str, str]] = []

    async def _send_text(to, msg, *a, **k):
        sent.append((to, msg))
        return {"ok": True}

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(ws, "_send_text", _send_text)
    monkeypatch.setattr(ws, "_resolve_reviewer",
                        lambda conn, phone: _wrap({"user_id": 7, "role_name": "npd_team"}))
    monkeypatch.setattr(ws, "_pop_pending", _none)
    monkeypatch.setattr(ws, "_pop_promote_pending", _none)
    monkeypatch.setattr(ws, "_promote_for_wamid", _none)
    monkeypatch.setattr(ws, "maintenance_forward_url", lambda: "https://maintenance.example/webhook")

    from app.modules.purchase.services import po_intimation as po
    monkeypatch.setattr(po, "handle_po_intimation_tap", _none)
    from app.modules.customer_returns.services import wa_notify as cr
    monkeypatch.setattr(cr, "handle_return_button_tap", _none)
    return sent


def _wrap(value):
    async def _coro():
        return value
    return _coro()


def _inbound(conn, text, *, context_id=None, raw=None):
    return asyncio.run(ws.handle_inbound(
        conn, from_phone=REVIEWER_PHONE, text=text, context_id=context_id, raw=raw))


# ── the reported thread ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["hi", "Maintenance", "Raise Complaint", "Compressor 3"])
def test_reviewer_small_talk_gets_no_npd_reply(wa, text):
    """The exact messages from the thread. The maintenance bot owns these."""
    res = _inbound(_Conn(), text)
    assert wa == [], f"NPD answered {text!r} with: {wa}"
    assert res == {"ok": True, "forwarded": "maintenance"}


def test_the_accept_hold_hint_is_what_used_to_leak(wa):
    _inbound(_Conn(), "hi")
    assert not any("ACCEPT" in m for _, m in wa), wa


# ── real NPD traffic still answered ──────────────────────────────────────────

def test_a_reply_quoting_an_npd_review_card_still_gets_the_hint(wa):
    """Addressed to US — the reviewer is fumbling the syntax on our own card, so
    the hint is exactly what they need."""
    res = _inbound(_Conn([REVIEW_WAMID]), "what do I do", context_id=REVIEW_WAMID)
    assert res == {"ok": False, "reason": "unparsed"}
    assert len(wa) == 1 and "ACCEPT" in wa[0][1]


def test_a_reply_quoting_someone_elses_card_is_left_alone(wa):
    """context_id present but not one of our review cards — not our conversation."""
    res = _inbound(_Conn([REVIEW_WAMID]), "ok", context_id="wamid.maintenance.card")
    assert wa == []
    assert res == {"ok": True, "forwarded": "maintenance"}


@pytest.mark.parametrize("text", ["ACCEPT 12345678", "accept 12345678",
                                  "HOLD 12345678", "APPROVE 12345678"])
def test_typed_commands_still_reach_the_npd_flow(wa, text):
    """Must NOT be swallowed as maintenance chatter — these resolve a request, and
    with the stub conn returning no row they answer 'Couldn't find request'."""
    res = _inbound(_Conn(), text)
    assert res["reason"] == "not_found"
    assert len(wa) == 1 and "Couldn't find request" in wa[0][1]


# ── deployments with no maintenance bot keep the old behaviour ───────────────

def test_without_a_maintenance_bot_the_hint_is_still_sent(wa, monkeypatch):
    """Nothing else would answer, so silence would be a regression."""
    monkeypatch.setattr(ws, "maintenance_forward_url", lambda: "")
    res = _inbound(_Conn(), "hi")
    assert res == {"ok": False, "reason": "unparsed"}
    assert len(wa) == 1 and "ACCEPT" in wa[0][1]
