"""Visitor-approval taps on the shared WABA must never reach the NPD review flow.

Guards the router's discriminator only (pure functions, no DB/network): if these
break, an approver gets "this number isn't recognised as an NPD reviewer" instead
of their visitor being approved.
"""
import asyncio
import os

from app.modules.sample.services import whatsapp_service as ws


def _envelope(msg: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


TEMPLATE_TAP = {"id": "wamid.1", "from": "919876543210", "type": "button",
                "button": {"payload": "approve_20260728053356", "text": "Approve"}}
INTERACTIVE_TAP = {"id": "wamid.2", "from": "919876543210", "type": "interactive",
                   "interactive": {"type": "button_reply",
                                   "button_reply": {"id": "reject_142", "title": "Reject"}}}
# NPD/promote quick replies carry the button TEXT, no "_<digits>" — must stay in the ERP.
PROMOTE_TAP = {"id": "wamid.3", "from": "919876543210", "type": "button",
               "button": {"payload": "Approve", "text": "Approve"}}


def test_visitor_taps_matched_in_both_button_shapes():
    assert [m["id"] for m in ws.visitor_approval_messages(_envelope(TEMPLATE_TAP))] == ["wamid.1"]
    assert [m["id"] for m in ws.visitor_approval_messages(_envelope(INTERACTIVE_TAP))] == ["wamid.2"]


def test_promote_tap_is_not_stolen_by_the_visitor_route():
    assert ws.visitor_approval_messages(_envelope(PROMOTE_TAP)) == []


def test_extract_messages_carries_payload_and_raw():
    m = ws.extract_messages(_envelope(TEMPLATE_TAP))[0]
    assert m["payload"] == "approve_20260728053356"   # logged, so routing is diagnosable
    assert m["raw"] is TEMPLATE_TAP                   # forwardable at the NPD dead-end
    assert ws.extract_messages(_envelope(INTERACTIVE_TAP))[0]["payload"] == "reject_142"


def test_forward_url_is_read_at_call_time():
    """The regression that broke this in prod: main.py's lifespan hydrates .env into
    os.environ AFTER this module is imported, so a module-level constant froze to ""
    and the whole forwarding path silently no-opped on every .env-based deploy."""
    prev = os.environ.pop("VISITOR_APPROVAL_FORWARD_URL", None)
    try:
        assert ws.visitor_forward_url() == ""
        os.environ["VISITOR_APPROVAL_FORWARD_URL"] = "  https://example/webhook  "
        assert ws.visitor_forward_url() == "https://example/webhook"
    finally:
        os.environ.pop("VISITOR_APPROVAL_FORWARD_URL", None)
        if prev is not None:
            os.environ["VISITOR_APPROVAL_FORWARD_URL"] = prev


def test_settings_declares_the_forward_url():
    """pydantic-settings ignores undeclared keys, so without this field the .env line
    never reaches Settings and the lifespan has nothing to hydrate."""
    from app.config import Settings
    assert "VISITOR_APPROVAL_FORWARD_URL" in Settings.model_fields


class _NoRowsConn:
    """asyncpg-shaped stub where the sender matches nothing: no reviewer, no promote
    gate, no pending state — i.e. someone who is not an ERP user at all."""
    async def fetchrow(self, *a, **k): return None
    async def fetchval(self, *a, **k): return None
    async def fetch(self, *a, **k): return []
    async def execute(self, *a, **k): return None


def _run_unknown_sender(raw: dict, text: str) -> tuple[dict, list, list]:
    """handle_inbound for a sender the ERP doesn't know; returns (result, forwarded, replies)."""
    forwarded, replies = [], []

    async def _fake_forward(messages, signature=None):
        forwarded.extend(messages)
        return len(messages)

    async def _fake_send(to, body):
        replies.append(body)

    orig_forward, orig_send = ws.forward_visitor_approvals, ws._send_text
    ws.forward_visitor_approvals, ws._send_text = _fake_forward, _fake_send
    try:
        result = asyncio.run(ws.handle_inbound(
            _NoRowsConn(), from_phone="919876543210", text=text,
            context_id=None, raw=raw))
    finally:
        ws.forward_visitor_approvals, ws._send_text = orig_forward, orig_send
    return result, forwarded, replies


def test_visitor_tap_from_unknown_sender_still_goes_to_visitor():
    """The original bug (4e00810): an approver got "isn't recognised as an NPD reviewer"
    for everything they sent. Their approve_/reject_ taps must still reach the visitor
    system, silently."""
    for raw in (TEMPLATE_TAP, INTERACTIVE_TAP):
        result, forwarded, replies = _run_unknown_sender(raw, "Approve")
        assert result == {"ok": True, "forwarded": "visitor"}, result
        assert forwarded == [raw]
        assert replies == []


def test_plain_text_goes_to_maintenance_never_to_visitor():
    """The collision fix. 4e00810 forwarded EVERY unattributed inbound to visitor, so
    once the maintenance relay went live a plain "Hi" reached visitor AND the maintenance
    bot and both answered. Visitor now only ever receives approve_/reject_; everything
    else is left to maintenance and the ERP stays silent."""
    prev = os.environ.get("MAINTENANCE_FORWARD_URL")
    os.environ["MAINTENANCE_FORWARD_URL"] = "https://example/api/whatsapp/webhook"
    try:
        for body in ("hello", "Hi", "APPROVE", "Compressor 3 is down"):
            raw = {"id": "wamid.x", "from": "919876543210", "type": "text",
                   "text": {"body": body}}
            result, forwarded, replies = _run_unknown_sender(raw, body)
            assert result == {"ok": True, "forwarded": "maintenance"}, (body, result)
            assert forwarded == [], (body, forwarded)    # visitor must NOT see it
            assert replies == [], (body, replies)        # and we must not answer either
    finally:
        os.environ.pop("MAINTENANCE_FORWARD_URL", None)
        if prev is not None:
            os.environ["MAINTENANCE_FORWARD_URL"] = prev


def test_unknown_sender_still_gets_the_npd_reply_when_forwarding_is_off():
    """Single-tenant deploys (neither forward URL set) must behave exactly as before."""
    prev = os.environ.pop("MAINTENANCE_FORWARD_URL", None)

    async def _no_forward(messages, signature=None):
        return 0

    replies = []

    async def _fake_send(to, body):
        replies.append(body)

    orig_forward, orig_send = ws.forward_visitor_approvals, ws._send_text
    ws.forward_visitor_approvals, ws._send_text = _no_forward, _fake_send
    try:
        result = asyncio.run(ws.handle_inbound(
            _NoRowsConn(), from_phone="919876543210", text="hello",
            context_id=None, raw={"id": "w", "from": "919876543210", "type": "text"}))
    finally:
        ws.forward_visitor_approvals, ws._send_text = orig_forward, orig_send
        if prev is not None:
            os.environ["MAINTENANCE_FORWARD_URL"] = prev
    assert result == {"ok": False, "reason": "unauthorised"}
    assert replies == ["Sorry, this number isn't recognised as an NPD reviewer."]


def test_text_message_has_no_button_payload():
    text_msg = {"id": "wamid.4", "from": "919876543210", "type": "text",
                "text": {"body": "hold 12345678"}}
    assert ws.extract_messages(_envelope(text_msg))[0]["payload"] is None
    assert ws.visitor_approval_messages(_envelope(text_msg)) == []
