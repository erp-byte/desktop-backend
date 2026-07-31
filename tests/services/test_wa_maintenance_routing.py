"""Maintenance-bot traffic on the shared WABA must reach the maintenance backend —
and the relay must not disturb, or be stolen by, the NPD / promote / visitor flows.

Pure functions only (no DB, no network) except the one httpx-stubbed test that pins
the byte-identity contract: we forward the ORIGINAL request bytes, so the
X-Hub-Signature-256 we pass through still validates on the maintenance side.
"""
import asyncio
import json
import os
import sys

if __name__ == "__main__":                     # runnable without pytest (not installed here):
    sys.path.insert(0, os.path.dirname(       #   python tests/services/test_wa_maintenance_routing.py
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.modules.sample.services import whatsapp_service as ws


def _envelope(*msgs: dict) -> dict:
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "102290129340398", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "918000000000",
                             "phone_number_id": "1013172898549548"},
                "contacts": [{"profile": {"name": "Fitter"}, "wa_id": "919876543210"}],
                "messages": list(msgs)}}]}]}


def _tap(mid: str, bid: str, *, shape: str = "button_reply") -> dict:
    return {"id": mid, "from": "919876543210", "type": "interactive",
            "interactive": {"type": shape, shape: {"id": bid, "title": "Report issue"}}}


TEXT = {"id": "wamid.t", "from": "919876543210", "type": "text",
        "text": {"body": "Compressor 3"}}
IMAGE = {"id": "wamid.i", "from": "919876543210", "type": "image",
         "image": {"id": "media.1", "mime_type": "image/jpeg"}}
# Must stay with the ERP / visitor: NPD + promote quick replies carry the button TEXT,
# visitor carries "approve_<digits>".
NPD_TAP = {"id": "wamid.n", "from": "919876543210", "type": "button",
           "button": {"payload": "Accept", "text": "Accept"}}
PROMOTE_TAP = {"id": "wamid.p", "from": "919876543210", "type": "button",
               "button": {"payload": "Approve", "text": "Approve"}}
VISITOR_TAP = {"id": "wamid.v", "from": "919876543210", "type": "button",
               "button": {"payload": "approve_20260728053356", "text": "Approve"}}


def test_every_declared_prefix_matches():
    for pfx in ("mod:", "mnt:", "qc:", "inv:", "hr:", "it:", "tkt:"):
        assert ws.is_maintenance_message(_tap("wamid.x", pfx + "1234")), pfx


def test_prefixed_taps_matched_in_all_three_button_shapes():
    assert ws.is_maintenance_message(_tap("a", "mnt:open"))                      # button_reply
    assert ws.is_maintenance_message(_tap("b", "tkt:42", shape="list_reply"))    # list_reply
    # Their FIRST message to a user must be a template (24h window); a template quick
    # reply arrives as type "button" with button.payload, not as "interactive".
    assert ws.is_maintenance_message(
        {"id": "c", "type": "button", "button": {"payload": "mnt:new", "text": "New ticket"}})


def test_erp_and_visitor_taps_are_never_stolen():
    for m in (NPD_TAP, PROMOTE_TAP, VISITOR_TAP):
        assert not ws.is_maintenance_message(m), m["button"]["payload"]


def test_plain_text_and_image_are_forwarded():
    """The bot's form answers (asset name, fault description, problem photo) carry no
    prefix at all — type alone is the only thing left to match on."""
    assert ws.has_maintenance_message(_envelope(TEXT))
    assert ws.has_maintenance_message(_envelope(IMAGE))


def test_status_callback_body_is_not_forwarded():
    """Delivery/read receipts live under value.statuses with no `messages` — the
    maintenance team explicitly does not want these."""
    body = {"entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.s", "status": "delivered"}]}}]}]}
    assert not ws.has_maintenance_message(body)


def test_body_of_only_erp_traffic_is_not_forwarded():
    assert not ws.has_maintenance_message(_envelope(NPD_TAP, PROMOTE_TAP, VISITOR_TAP))


def test_mixed_batch_forwards_the_whole_body():
    """Meta rarely batches across conversations, but when it does we cannot forward a
    subset without rebuilding (and thus invalidating) the signed body. Documented
    trade-off: the maintenance side ignores what isn't theirs."""
    assert ws.has_maintenance_message(_envelope(NPD_TAP, _tap("m", "mnt:open")))


def test_forward_url_is_read_at_call_time():
    """The regression that broke visitor forwarding in prod: main.py's lifespan hydrates
    .env into os.environ AFTER this module is imported, so a module-level constant froze
    to "" and the whole path silently no-opped on every .env-based deploy."""
    prev = os.environ.pop("MAINTENANCE_FORWARD_URL", None)
    try:
        assert ws.maintenance_forward_url() == ""
        os.environ["MAINTENANCE_FORWARD_URL"] = "  https://example/api/whatsapp/webhook  "
        assert ws.maintenance_forward_url() == "https://example/api/whatsapp/webhook"
    finally:
        os.environ.pop("MAINTENANCE_FORWARD_URL", None)
        if prev is not None:
            os.environ["MAINTENANCE_FORWARD_URL"] = prev


def test_settings_declares_the_new_keys():
    """pydantic-settings ignores undeclared keys, so without these fields the .env lines
    never reach Settings and the lifespan has nothing to hydrate."""
    from app.config import Settings
    for k in ("MAINTENANCE_FORWARD_URL", "MAINTENANCE_FORWARD_TYPES", "WHATSAPP_APP_SECRET"):
        assert k in Settings.model_fields, k


def test_main_hydrates_the_new_keys():
    """Third leg of the three-place rule — a Settings field nobody copies into
    os.environ is just as dead as no field at all."""
    from pathlib import Path
    # …/app/modules/sample/services/whatsapp_service.py → parents[3] is app/
    src = Path(ws.__file__).parents[3].joinpath("main.py").read_text(encoding="utf-8")
    for k in ("MAINTENANCE_FORWARD_URL", "MAINTENANCE_FORWARD_TYPES", "WHATSAPP_APP_SECRET"):
        assert f'"{k}"' in src, k


def test_no_url_is_a_total_noop():
    prev = os.environ.pop("MAINTENANCE_FORWARD_URL", None)
    try:
        assert ws.forward_maintenance(b"{}", _envelope(TEXT), "sha256=deadbeef") is False
    finally:
        if prev is not None:
            os.environ["MAINTENANCE_FORWARD_URL"] = prev


def test_forwarded_bytes_are_identical_and_signature_passed_through():
    """THE contract. If this fails, the maintenance backend's signature check breaks:
    the header signs Meta's exact bytes, so any re-serialisation (httpx `json=`) that
    reorders keys or changes spacing invalidates it."""
    body = _envelope(_tap("wamid.1", "mnt:open"))
    # Key order deliberately not what json.dumps(sort_keys=True) would produce.
    raw = json.dumps(body, separators=(",", ":")).encode()
    sig = "sha256=abc123"
    seen = {}

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            seen.update(url=url, kw=kw)
            class _R:
                status_code = 200
                text = "ok"
            return _R()

    prev = os.environ.get("MAINTENANCE_FORWARD_URL")
    orig = ws.httpx.AsyncClient
    os.environ["MAINTENANCE_FORWARD_URL"] = "https://example/api/whatsapp/webhook"
    ws.httpx.AsyncClient = _FakeClient
    try:
        asyncio.run(ws._post_maintenance(raw, sig))
    finally:
        ws.httpx.AsyncClient = orig
        os.environ.pop("MAINTENANCE_FORWARD_URL", None)
        if prev is not None:
            os.environ["MAINTENANCE_FORWARD_URL"] = prev

    assert seen["url"] == "https://example/api/whatsapp/webhook"
    assert seen["kw"]["content"] == raw                      # byte-identical, not rebuilt
    assert "json" not in seen["kw"]                          # would re-serialise
    assert seen["kw"]["headers"]["X-Hub-Signature-256"] == sig
    # metadata / contacts / entry.id survive — nothing trimmed.
    entry = json.loads(seen["kw"]["content"])["entry"][0]
    assert entry["id"] == "102290129340398"
    value = entry["changes"][0]["value"]
    assert value["metadata"]["phone_number_id"] == "1013172898549548"
    assert value["contacts"][0]["wa_id"] == "919876543210"


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS  {_name}")
            except Exception as _e:  # noqa: BLE001
                _failed += 1
                print(f"FAIL  {_name}: {_e!r}")
    print(f"\n{_failed} failure(s)")
    sys.exit(1 if _failed else 0)
