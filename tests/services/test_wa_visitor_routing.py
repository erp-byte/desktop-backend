"""Visitor-approval taps on the shared WABA must never reach the NPD review flow.

Guards the router's discriminator only (pure functions, no DB/network): if these
break, an approver gets "this number isn't recognised as an NPD reviewer" instead
of their visitor being approved.
"""
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


def test_text_message_has_no_button_payload():
    text_msg = {"id": "wamid.4", "from": "919876543210", "type": "text",
                "text": {"body": "hold 12345678"}}
    assert ws.extract_messages(_envelope(text_msg))[0]["payload"] is None
    assert ws.visitor_approval_messages(_envelope(text_msg)) == []
