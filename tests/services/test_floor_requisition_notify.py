"""notify_service: who store notices go to, what they say, and that nothing in
them can fail a request. No database, SMTP server or Graph API is touched."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.modules.floor_requisition.services import notify_service as N

RAISED = {
    "requisition_id": 87654321, "job_card_id": 3958260, "warehouse": "W202", "floor": "First Floor",
    "material_sku_name": "California Pista Inshell Roasted and Salted", "item_type": "RM",
    "requested_qty": 88.2, "requested_unit": "kg", "required_qty": 250.0, "required_unit": "kg",
    "available_qty": 161.8, "available_unit": "kg", "shortage_qty": 88.2, "shortage_unit": "kg",
    "note": "Needed for\nevening\tshift", "raised_by": "Ravi K", "raised_at": "2026-09-15T08:35:00+00:00",
    "status": "raised",
}
CARD = {"job_card_id": 3958260, "job_card_number": "PLAN-145504-L145549-S1",
        "fg_sku_name": "Carnival Roasted & Salted Pistachio 250g", "customer_name": "Glorious Enterprise",
        "batch_number": "P145504-L145549-S1", "process_name": "Sorting + Packing", "stage": "packaging",
        "status": "in_progress", "entity": "cfpl"}


def _user(uid, name, *, email="", phone="", warehouses=None, floors=None):
    return {"user_id": uid, "full_name": name, "email": email, "phone": phone,
            "allowed_warehouses": warehouses, "allowed_floors": floors}


# ── place coverage ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("warehouses, floors, expected", [
    (None, None, True),                                   # no grants = everywhere
    ([], [], True),
    (["W-202"], None, True),                              # both warehouse spellings
    (["W202", "A185"], ["first floor"], True),            # floors ignore case
    (["A185"], None, False),                              # other plant
    (["W202"], ["Terrace"], False),                       # other floor
    ([" "], ["  "], True),                                # blank entries are no limit
])
def test_covers_place_matches_place_scope(warehouses, floors, expected):
    assert N.covers_place(warehouses, floors, "W202", "First Floor") is expected


class _Conn:
    def __init__(self, users=(), cards=(), boom=False):
        self.users, self.cards, self.boom, self.calls = list(users), list(cards), boom, []

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.calls.append((s, args))
        if self.boom:
            raise RuntimeError("database went away")
        if "FROM auth_user u" in s:
            return self.users
        if "FROM job_card_v2" in s:
            return self.cards
        raise AssertionError(f"unexpected fetch: {s[:80]}")


class _Pool:
    def __init__(self, conn):
        self.conn, self.released = conn, 0

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                pool.released += 1
                return False
        return _Ctx()


def test_recipients_are_store_heads_covering_the_place_once_each():
    conn = _Conn(users=[
        _user(1, "Kaushal Patil", email="k@candorfoods.in", warehouses=["W202", "A185"]),
        _user(1, "Kaushal Patil", email="k@candorfoods.in", warehouses=["W202", "A185"]),  # defensive dedupe
        _user(2, "A185 Store", email="a@candorfoods.in", warehouses=["A185"]),
        _user(3, "Terrace Store", email="t@candorfoods.in", warehouses=["W202"], floors=["Terrace"]),
        _user(4, "Everywhere", phone="98765 43210"),
    ])
    holders = asyncio.run(N.store_role_holders(conn))
    assert [h["name"] for h in holders] == ["Kaushal Patil", "A185 Store", "Terrace Store", "Everywhere"]
    assert [p["name"] for p in N.covering(holders, "W202", "First Floor")] == ["Kaushal Patil", "Everywhere"]
    sql, args = conn.calls[0]
    assert args == (["store_head"],)


def test_recipient_query_matches_how_sign_in_resolves_accounts_and_roles():
    sql = " ".join(N._RECIPIENTS_SQL.split())
    # validate_session: an active account whose status is 'active' (suspended users are left out)
    assert "u.is_active" in sql and "COALESCE(u.status, 'active') = 'active'" in sql
    # _effective_roles: auth_user_role decides; the primary role counts only without any rows there
    assert ("EXISTS (SELECT 1 FROM auth_user_role ur JOIN auth_role r ON r.role_id = ur.role_id "
            "WHERE ur.user_id = u.user_id AND r.role_name = ANY($1::text[]))") in sql
    assert ("NOT EXISTS (SELECT 1 FROM auth_user_role ur WHERE ur.user_id = u.user_id) "
            "AND EXISTS (SELECT 1 FROM auth_role r WHERE r.role_id = u.role_id AND r.role_name = ANY($1::text[]))") in sql


# ── email ───────────────────────────────────────────────────────────────────

def test_email_names_the_request_the_job_card_and_the_store_screen():
    subject, body = N.compose_email(RAISED, CARD, "https://erpcf.in/")
    assert subject == ("Material request #87654321 - California Pista Inshell Roasted and Salted "
                       "for PLAN-145504-L145549-S1")
    for text in ("#87654321", "Ravi K, 15 Sep 2026, 14:05", "PLAN-145504-L145549-S1",
                 "Carnival Roasted & Salted Pistachio 250g - Glorious Enterprise", "W202 · First Floor",
                 "California Pista Inshell Roasted and Salted (RM)", "Requested     : 88.200 kg",
                 "Required      : 250.000 kg", "Fresh stock   : 161.800 kg", "Shortage      : 88.200 kg",
                 "https://erpcf.in/modules/stores/production-indents"):
        assert text in body, text


def test_email_without_a_job_card_record_still_reads():
    req = {**RAISED, "note": None, "required_qty": None, "shortage_qty": None,
           "requested_qty": 1000, "requested_unit": "pcs"}
    subject, body = N.compose_email(req, None, "https://erpcf.in")
    assert "for #3958260" in subject
    assert "Requested     : 1,000 pcs" in body and "Required      : -" in body
    assert "Note" not in body


# ── WhatsApp: the approved template floor_requisition_raised_store ──────────

def test_template_message_matches_the_approved_template_exactly():
    msg = N.template_message("919876543210", RAISED, CARD)
    assert msg["type"] == "template" and msg["to"] == "919876543210"
    tpl = msg["template"]
    assert tpl["name"] == "floor_requisition_raised_store" and tpl["language"] == {"code": "en"}
    header, body, accept, hold = tpl["components"]
    assert header == {"type": "header", "parameters": [{"type": "text", "text": "PLAN-145504-L145549-S1"}]}
    assert [p["text"] for p in body["parameters"]] == [
        "87654321",                                        # {{1}} requisition no
        "PLAN-145504-L145549-S1",                          # {{2}} job card
        "Carnival Roasted & Salted Pistachio 250g",        # {{3}} FG
        "Glorious Enterprise",                             # {{4}} customer
        "California Pista Inshell Roasted and Salted (RM)",  # {{5}} material
        "88.200 kg",                                       # {{6}} quantity requested
        "88.200 kg",                                       # {{7}} short on the floor
        "W202 · First Floor",                         # {{8}} deliver to
        "Ravi K",                                          # {{9}} requested by
        "15 Sep 2026, 14:05",                              # {{10}} raised at (IST)
        "Needed for evening shift",                        # {{11}} note, one line
    ]
    assert accept == {"type": "button", "sub_type": "quick_reply", "index": "0",
                      "parameters": [{"type": "payload", "payload": "floor_req:accept:87654321"}]}
    assert hold == {"type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "floor_req:hold:87654321"}]}


def test_template_values_are_never_blank_or_multiline():
    params = N.body_params({**RAISED, "note": None, "raised_by": "  Ravi \n K ", "shortage_qty": None}, None)
    assert params[1] == "3958260" and params[2] == "-" and params[3] == "-"   # no job card record
    assert params[6] == "-" and params[8] == "Ravi K" and params[10] == "-"
    assert all(p and "\n" not in p and "\t" not in p and "    " not in p for p in params)


def test_header_stays_inside_metas_60_characters():
    fixed = len("Material requested — Job card ")
    long_card = {**CARD, "job_card_number": "PLAN-99282651-L99282655-S2-EXTRA-LONG-NUMBER"}
    header = N.header_param(RAISED, long_card)
    assert header == "3958260"                                  # falls back to the id, never cut
    assert fixed + len(N.header_param(RAISED, CARD)) <= 60


def test_body_values_together_stay_under_metas_1024_characters():
    huge = {**RAISED, "material_sku_name": "x" * 500, "raised_by": "y" * 300, "warehouse": "W" * 200,
            "floor": "F" * 200, "note": "n" * 500, "requested_qty": 99999999999.999, "shortage_qty": 99999999999.999}
    card = {**CARD, "job_card_number": "J" * 200, "fg_sku_name": "P" * 400, "customer_name": "C" * 400}
    params = N.body_params(huge, card)
    assert len(params) == 11
    assert sum(len(p) for p in params) <= 700
    fixed_body = 265   # the approved body without its variables is 265 characters
    assert fixed_body + sum(len(p) for p in params) <= 1024


# ── sending ─────────────────────────────────────────────────────────────────

def _settings(**over):
    base = dict(SMTP_HOST="smtp.example", WEB_APP_URL="https://erpcf.in", WHATSAPP_ENABLED=True,
                WHATSAPP_ACCESS_TOKEN="token", WHATSAPP_PHONE_NUMBER_ID="123",
                WHATSAPP_GRAPH_BASE="https://graph.example/v21.0")
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def channels(monkeypatch):
    sent = {"mail": [], "wa": []}

    def fake_send(subject, body, to, cc, **kw):
        sent["mail"].append({"subject": subject, "to": to, "cc": cc, **kw})
        return True

    async def fake_wa(settings, message):
        if message["to"].endswith("0000"):
            raise RuntimeError("HTTP 400 - (#131026) Message undeliverable")
        sent["wa"].append(message)

    monkeypatch.setattr(N.mail_service, "_send", fake_send)
    monkeypatch.setattr(N, "_send_whatsapp_template", fake_wa)
    return sent


def _pool(users):
    return _Pool(_Conn(users=users, cards=[CARD]))


def test_email_and_whatsapp_go_to_the_store_users_covering_the_place(channels, monkeypatch):
    monkeypatch.setattr(N, "_settings", lambda: _settings())
    pool = _pool([_user(1, "Kaushal Patil", email="k@candorfoods.in", phone="9876543210", warehouses=["W202"]),
                  _user(2, "A185 Store", email="a@candorfoods.in", phone="9123456789", warehouses=["A185"])])
    report = asyncio.run(N.notify_store_of_raise(pool, RAISED))

    assert report["recipients"] == ["Kaushal Patil"]
    assert channels["mail"] == [{
        "subject": "Material request #87654321 - California Pista Inshell Roasted and Salted for PLAN-145504-L145549-S1",
        "to": ["k@candorfoods.in"], "cc": [], "entity_type": "FloorRequisition", "entity_id": "87654321",
        "event": "raised", "status": "raised", "actor": "Ravi K"}]
    assert report["email"]["to"] == ["k@candorfoods.in"]
    assert [m["to"] for m in channels["wa"]] == ["919876543210"]
    assert channels["wa"][0] == N.template_message("919876543210", RAISED, CARD)
    assert report["whatsapp"] == {"sent": 1, "failed": [], "skipped": None}
    assert pool.released == 1 and report["error"] is None


def test_whatsapp_dedupes_phones_and_survives_a_bad_number(channels, monkeypatch):
    monkeypatch.setattr(N, "_settings", lambda: _settings())
    pool = _pool([_user(1, "Kaushal Patil", phone="98765 43210"),
                  _user(2, "Bad Number", phone="+91 99999 90000"),
                  _user(3, "Same Phone", phone="+919876543210")])
    report = asyncio.run(N.notify_store_of_raise(pool, RAISED))

    assert [m["to"] for m in channels["wa"]] == ["919876543210"]      # normalised, deduped
    assert report["whatsapp"]["sent"] == 1
    assert [f["phone"] for f in report["whatsapp"]["failed"]] == ["919999990000"]
    assert report["email"]["skipped"] == "no_email_on_store_users" and channels["mail"] == []


@pytest.mark.parametrize("over, reason", [
    ({"WHATSAPP_ENABLED": False}, "whatsapp_disabled"),
    ({"WHATSAPP_ACCESS_TOKEN": " "}, "whatsapp_credentials_missing"),
    ({"WHATSAPP_PHONE_NUMBER_ID": ""}, "whatsapp_credentials_missing"),
])
def test_whatsapp_stays_off_without_its_switches(channels, monkeypatch, over, reason):
    monkeypatch.setattr(N, "_settings", lambda: _settings(**over))
    report = asyncio.run(N.notify_store_of_raise(_pool([_user(1, "K", phone="9876543210")]), RAISED))
    assert report["whatsapp"]["skipped"] == reason and channels["wa"] == []


def test_no_smtp_host_means_no_email(channels, monkeypatch):
    monkeypatch.setattr(N, "_settings", lambda: _settings(SMTP_HOST=""))
    report = asyncio.run(N.notify_store_of_raise(_pool([_user(1, "K", email="k@x.in")]), RAISED))
    assert report["email"]["skipped"] == "smtp_not_configured" and channels["mail"] == []


def test_nobody_covering_the_place_sends_nothing(channels, monkeypatch):
    monkeypatch.setattr(N, "_settings", lambda: pytest.fail("settings are not needed when nobody is told"))
    report = asyncio.run(N.notify_store_of_raise(_pool([_user(2, "A185 Store", email="a@x.in", warehouses=["A185"])]), RAISED))
    assert report["recipients"] == [] and channels == {"mail": [], "wa": []}
    assert report["email"]["skipped"] == "no_store_user_for_this_place"


def test_no_store_head_users_at_all_is_reported_as_such_and_warned(channels, monkeypatch, caplog):
    monkeypatch.setattr(N, "_settings", lambda: pytest.fail("settings are not needed when nobody is told"))
    with caplog.at_level("WARNING", logger=N.logger.name):
        report = asyncio.run(N.notify_store_of_raise(_pool([]), RAISED))
    assert report["email"]["skipped"] == report["whatsapp"]["skipped"] == "no_active_store_head_users"
    assert channels == {"mail": [], "wa": []}
    assert any(r.levelname == "WARNING" and "store_head" in r.getMessage() for r in caplog.records)


def test_a_mail_server_refusal_is_not_reported_as_sent(channels, monkeypatch):
    monkeypatch.setattr(N, "_settings", lambda: _settings(WHATSAPP_ENABLED=False))
    monkeypatch.setattr(N.mail_service, "_send", lambda *a, **kw: False)
    report = asyncio.run(N.notify_store_of_raise(_pool([_user(1, "K", email="k@x.in")]), RAISED))
    assert report["email"] == {"to": [], "skipped": "send_failed"}


def test_a_failure_anywhere_is_reported_never_raised(channels, monkeypatch):
    monkeypatch.setattr(N, "_settings", lambda: _settings())
    report = asyncio.run(N.notify_store_of_raise(_Pool(_Conn(boom=True)), RAISED))
    assert report["error"] == "database went away" and channels == {"mail": [], "wa": []}

    def mail_down(*a, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr(N.mail_service, "_send", mail_down)
    report = asyncio.run(N.notify_store_of_raise(_pool([_user(1, "K", email="k@x.in")]), RAISED))
    assert report["email"]["skipped"] == "error: smtp down" and report["error"] is None
