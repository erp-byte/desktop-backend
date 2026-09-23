"""job_card_notify: who is told about a new job card, what the notice says, and
that nothing in it can fail the request that created the card. No database, SMTP
server or Graph API is touched."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.modules.floor_requisition.services import notify_service as STORE
from app.modules.production.services import job_card_notify as J

CARD = {
    "job_card_id": 39582601, "job_card_number": "PLAN-145504-L145549-S1",
    "fg_sku_name": "Carnival Roasted & Salted Pistachio 250g", "customer_name": "Glorious Enterprise",
    "planned_qty_kg": 64.0, "planned_qty_units": 640.0, "step_number": 1,
    "process_name": "Flavouring + Mixing", "factory": "A-185", "floor": "Roasting Area",
    "input_kind": "RM", "input_code": None, "is_locked": True,
    "locked_reason": "awaiting_previous_stage", "status": "locked",
    "created_at": "2026-09-23T05:12:00+00:00", "created_by": "Ravi K",
}
PACKING = {**CARD, "job_card_id": 39582602, "job_card_number": "PLAN-145504-L145549-S2",
           "step_number": 2, "process_name": "Sorting + Packing", "floor": "Packing Floor",
           "input_kind": "SFG", "input_code": "SFG0042", "is_locked": False,
           "locked_reason": None, "status": "unlocked"}


def _user(uid, name, *, email="", phone="", warehouses=None, floors=None):
    """A floor manager. The grants default to the CARD's place, because only the
    floor's own manager is notified — a test about e-mail or WhatsApp would
    otherwise be silently testing "nobody to tell". Pass [] for no grants."""
    return {"user_id": uid, "full_name": name, "email": email, "phone": phone,
            "allowed_warehouses": ["A185"] if warehouses is None else warehouses,
            "allowed_floors": ["Roasting Area", "Packing Floor"] if floors is None else floors}


# ── place coverage: the card's factory is the warehouse axis ────────────────

@pytest.mark.parametrize("warehouses, floors, expected", [
    (None, None, False),                                  # unrestricted is NOT "this floor's manager"
    ([], [], False),
    (["A185"], None, False),                              # a plant grant alone is not a floor
    (["A-185", "W202"], ["roasting area"], True),         # the card says 'A-185' — hyphen-blind, floors ignore case
    ([], ["Roasting Area"], False),                       # a floor grant alone does not name the plant
    (["W202"], ["Roasting Area"], False),                 # other plant
    (["A185"], ["Terrace"], False),                       # other floor
    ([" "], ["  "], False),                               # blank entries grant nothing, so no notice
])
def test_only_the_floors_own_manager_is_notified(warehouses, floors, expected):
    """Stricter than place_scope, which the job card READERS use: there an empty
    grant means "no limit", which would make an unrestricted account the
    recipient of every card on every floor."""
    assert J.assigned_to_place(warehouses, floors, "A-185", "Roasting Area") is expected


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
            return [c for c in self.cards if c["job_card_id"] in args[0]]
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


# ── who ─────────────────────────────────────────────────────────────────────

def test_recipients_are_the_floors_own_managers_once_each():
    conn = _Conn(users=[
        _user(1, "Kaushal Patil", email="k@candorfoods.in", warehouses=["A185", "W202"],
              floors=["Roasting Area", "Packing Floor"]),
        _user(1, "Kaushal Patil", email="k@candorfoods.in", warehouses=["A185", "W202"],
              floors=["Roasting Area", "Packing Floor"]),                      # defensive dedupe
        _user(2, "W202 Floor", email="w@candorfoods.in", warehouses=["W202"], floors=["Roasting Area"]),
        _user(3, "Packing Manager", email="p@candorfoods.in", warehouses=["A185"], floors=["Packing Floor"]),
        _user(4, "Everywhere", phone="98765 43210", warehouses=[], floors=[]),  # no grants: never notified
    ])
    holders = asyncio.run(J.floor_role_holders(conn))
    assert [h["name"] for h in holders] == ["Kaushal Patil", "W202 Floor", "Packing Manager", "Everywhere"]
    assert [p["name"] for p in J.assigned(holders, "A-185", "Roasting Area")] == ["Kaushal Patil"]
    assert [p["name"] for p in J.assigned(holders, "A185", "Packing Floor")] == [
        "Kaushal Patil", "Packing Manager"]
    sql, args = conn.calls[0]
    assert args == (["floor_manager"],)


def test_recipient_query_matches_how_sign_in_resolves_accounts_and_roles():
    sql = " ".join(J._RECIPIENTS_SQL.split())
    # validate_session: an active account whose status is 'active' (suspended users are left out)
    assert "u.is_active" in sql and "COALESCE(u.status, 'active') = 'active'" in sql
    # _effective_roles: auth_user_role decides; the primary role counts only without any rows there
    assert ("EXISTS (SELECT 1 FROM auth_user_role ur JOIN auth_role r ON r.role_id = ur.role_id "
            "WHERE ur.user_id = u.user_id AND r.role_name = ANY($1::text[]))") in sql
    assert ("NOT EXISTS (SELECT 1 FROM auth_user_role ur WHERE ur.user_id = u.user_id) "
            "AND EXISTS (SELECT 1 FROM auth_role r WHERE r.role_id = u.role_id AND r.role_name = ANY($1::text[]))") in sql


def test_the_store_notice_and_this_one_share_their_plumbing():
    """One sanitiser, one Graph sender, one recipient rule — not two copies."""
    assert J._param is STORE._param
    assert J._send_whatsapp_template is STORE._send_whatsapp_template
    assert J._RECIPIENTS_SQL is STORE._RECIPIENTS_SQL
    # The place rule is the one thing they deliberately do NOT share: the store
    # notice follows place_scope (an empty grant is no limit), this one needs the
    # floor named, so an unrestricted account is not told about every card.
    assert J.assigned_to_place is not STORE.covers_place
    assert STORE.covers_place(None, None, "A-185", "Roasting Area") is True
    assert J.assigned_to_place(None, None, "A-185", "Roasting Area") is False


# ── WhatsApp: the approved template job_card_created_floor ─────────────────

def test_template_message_matches_the_approved_template_exactly():
    msg = J.template_message("919876543210", CARD)
    assert msg["messaging_product"] == "whatsapp"
    assert msg["type"] == "template" and msg["to"] == "919876543210"
    tpl = msg["template"]
    assert tpl["name"] == "job_card_created_floor" and tpl["language"] == {"code": "en"}
    header, body = tpl["components"]
    # The card's 8-digit id, not job_card_number: the number spells out the whole
    # chain, and the id is what the job card page is opened by.
    assert header == {"type": "header", "parameters": [{"type": "text", "text": "39582601"}]}
    assert [p["text"] for p in body["parameters"]] == [
        "39582601",                                        # {{1}} job card
        "Carnival Roasted & Salted Pistachio 250g",        # {{2}} product
        "Glorious Enterprise",                             # {{3}} customer
        "64.000 kg · 640 units",                           # {{4}} quantity
        "1 · Flavouring + Mixing",                         # {{5}} step
        "A-185 · Roasting Area",                           # {{6}} place
        "RM",                                              # {{7}} input
        "Ravi K on 23 Sep 2026",                           # {{8}} created by … (the template adds " at ")
        "10:42",                                           # {{9}} … the time, IST
        "Waiting for the previous stage",                  # {{10}} note
    ]


def test_the_template_has_no_buttons():
    """The approved template carries no quick replies — a button component would
    be refused by Meta, and there is no tap to handle."""
    components = J.template_message("919876543210", CARD)["template"]["components"]
    assert [c["type"] for c in components] == ["header", "body"]


def test_quantity_step_place_and_input_read_as_one_line_each():
    assert J.body_params({**CARD, "planned_qty_units": None})[3] == "64.000 kg"
    assert J.body_params({**CARD, "planned_qty_units": 0})[3] == "64.000 kg"
    assert J.body_params(PACKING)[4] == "2 · Sorting + Packing"
    assert J.body_params(PACKING)[5] == "A-185 · Packing Floor"
    assert J.body_params(PACKING)[6] == "SFG0042"          # the code it consumes, when it has one
    assert J.body_params({**CARD, "input_kind": None})[6] == "RM from store"


def test_the_note_says_why_a_locked_card_cannot_start_yet():
    assert J.body_params(CARD)[9] == "Waiting for the previous stage"
    assert J.body_params({**CARD, "locked_reason": "material_pending"})[9] == "material_pending"
    assert J.body_params(PACKING)[9] == "-"                # unlocked: nothing holding it


def test_template_values_are_never_blank_or_multiline():
    params = J.body_params({**CARD, "customer_name": None, "created_by": "  Ravi \n K ",
                            "process_name": "Flavouring\tand\tMixing",
                            "fg_sku_name": "Pista" + " " * 6 + "250g", "floor": None})
    assert params[2] == "-"                                # never empty
    assert params[1] == "Pista 250g"                       # no run of 4+ spaces
    assert params[4] == "1 · Flavouring and Mixing"        # tabs collapse
    assert params[5] == "A-185"
    assert params[7] == "Ravi K on 23 Sep 2026"
    assert all(p and "\n" not in p and "\t" not in p and "    " not in p for p in params)


def test_an_unknown_creator_or_time_still_sends():
    params = J.body_params({**CARD, "created_by": None, "created_at": None})
    assert params[7] == "-" and params[8] == "-"


def test_an_unknown_creator_never_puts_the_date_in_the_name_slot():
    """{{8}} names a person: with the time known but nobody to credit, the line
    must not read "Created by: 23 Sep 2026 at 10:42"."""
    anonymous = {**CARD, "created_by": None}
    params = J.body_params(anonymous)
    assert params[7] == "-" and params[8] == "10:42"
    _, body = J.compose_email(anonymous, "https://erpcf.in")
    assert "Created at : 10:42" in body and "Created by" not in body


def test_header_stays_inside_metas_60_characters():
    fixed = len("New job card — ")
    long_number = {**CARD, "job_card_number": "PLAN-99282651-L99282655-S2-EXTRA-LONG-NUMBER-HERE"}
    assert J.header_param(long_number) == "39582601"       # the id, whatever the number says
    assert J.header_param({**CARD, "job_card_id": None}) == "-"
    assert fixed + len(J.header_param(CARD)) <= 60


def test_body_values_together_stay_under_metas_1024_characters():
    huge = {**CARD, "job_card_number": "J" * 200, "fg_sku_name": "P" * 400, "customer_name": "C" * 400,
            "process_name": "S" * 300, "factory": "A" * 200, "floor": "F" * 200, "input_code": "I" * 200,
            "created_by": "R" * 300, "locked_reason": "L" * 400, "planned_qty_kg": 99999999999.999,
            "planned_qty_units": 99999999999.999}
    params = J.body_params(huge)
    assert len(params) == 10
    fixed_body = 164   # the approved body without its variables is 164 characters
    assert fixed_body + sum(len(p) for p in params) <= 1024


# ── email ───────────────────────────────────────────────────────────────────

def test_email_names_the_job_card_the_product_and_the_place():
    subject, body = J.compose_email(CARD, "https://erpcf.in/")
    assert subject == ("New job card 39582601 - Carnival Roasted & Salted Pistachio 250g "
                       "for A-185 · Roasting Area")
    for text in ("39582601", "Carnival Roasted & Salted Pistachio 250g", "Glorious Enterprise",
                 "64.000 kg · 640 units", "1 · Flavouring + Mixing", "A-185 · Roasting Area",
                 "Ravi K on 23 Sep 2026 at 10:42", "Waiting for the previous stage",
                 "https://erpcf.in/modules/job-card/39582601"):
        assert text in body, text


def test_email_without_a_number_or_a_note_still_reads():
    subject, body = J.compose_email({**PACKING, "job_card_number": None}, "https://erpcf.in")
    assert "New job card 39582602" in subject
    assert "Note" not in body
    assert "https://erpcf.in/modules/job-card/39582602" in body   # the card, not a guessed page


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
            raise RuntimeError("HTTP 400 - (#132000) template param count mismatch")
        sent["wa"].append(message)

    monkeypatch.setattr(J.mail_service, "_send", fake_send)
    monkeypatch.setattr(J, "_send_whatsapp_template", fake_wa)
    return sent


def _pool(users, cards=(CARD,)):
    return _Pool(_Conn(users=users, cards=cards))


def test_one_message_per_card_per_recipient(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings())
    pool = _pool([_user(1, "Kaushal Patil", email="k@candorfoods.in", phone="9876543210", warehouses=["A185"],
                      floors=["Roasting Area", "Packing Floor"]),
                  _user(2, "W202 Floor", email="w@candorfoods.in", phone="9123456789", warehouses=["W202"]),
                  _user(3, "Everywhere", email="e@candorfoods.in", phone="9988776655", warehouses=["A185"],
                      floors=["Roasting Area", "Packing Floor"])],
                 cards=(CARD, PACKING))
    report = asyncio.run(J.notify_floor_of_new_job_cards(
        pool, [CARD["job_card_id"], PACKING["job_card_id"]], created_by="Ravi K"))

    assert [c["job_card_id"] for c in report["cards"]] == [39582601, 39582602]
    assert [c["recipients"] for c in report["cards"]] == [["Kaushal Patil", "Everywhere"]] * 2
    # one WhatsApp per card per recipient, in card order
    assert [m["to"] for m in channels["wa"]] == ["919876543210", "919988776655"] * 2
    assert [m["template"]["components"][1]["parameters"][0]["text"] for m in channels["wa"]] == [
        "39582601", "39582601", "39582602", "39582602"]
    assert channels["wa"][0] == J.template_message("919876543210", {**CARD, "created_by": "Ravi K"})
    # one email per card, to everyone covering it
    assert [m["to"] for m in channels["mail"]] == [["k@candorfoods.in", "e@candorfoods.in"]] * 2
    assert [m["entity_id"] for m in channels["mail"]] == ["39582601", "39582602"]
    assert channels["mail"][0]["entity_type"] == "JobCard" and channels["mail"][0]["event"] == "created"
    assert channels["mail"][1]["subject"].startswith("New job card 39582602")
    assert [c["whatsapp"] for c in report["cards"]] == [{"sent": 2, "failed": [], "skipped": None}] * 2
    assert pool.released == 1 and report["error"] is None


def test_the_same_card_twice_in_one_request_is_notified_once(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings())
    pool = _pool([_user(1, "Kaushal Patil", email="k@candorfoods.in", phone="9876543210")])
    report = asyncio.run(J.notify_floor_of_new_job_cards(pool, [39582601, 39582601, "39582601"]))
    assert report["job_card_ids"] == [39582601]
    assert len(channels["wa"]) == 1 and len(channels["mail"]) == 1


def test_a_card_that_is_no_longer_there_is_reported_not_sent(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings())
    report = asyncio.run(J.notify_floor_of_new_job_cards(
        _pool([_user(1, "K", email="k@x.in", phone="9876543210")]), [39582601, 77777777]))
    assert report["missing"] == [77777777]
    assert len(channels["wa"]) == 1 and len(channels["mail"]) == 1


def test_a_recipient_without_a_phone_is_still_emailed(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings())
    report = asyncio.run(J.notify_floor_of_new_job_cards(_pool([
        _user(1, "No Phone", email="n@candorfoods.in"),
        _user(2, "Phone Only", phone="98765 43210"),
        _user(3, "Same Phone", phone="+919876543210")]), [39582601]))
    card = report["cards"][0]
    assert card["email"]["to"] == ["n@candorfoods.in"]
    assert [m["to"] for m in channels["wa"]] == ["919876543210"]      # normalised, deduped
    assert card["whatsapp"]["sent"] == 1


def test_a_graph_error_is_recorded_not_raised(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings())
    report = asyncio.run(J.notify_floor_of_new_job_cards(_pool([
        _user(1, "Good Number", phone="9876543210"),
        _user(2, "Bad Number", phone="+91 99999 90000")]), [39582601]))
    card = report["cards"][0]
    assert card["whatsapp"]["sent"] == 1
    assert [f["phone"] for f in card["whatsapp"]["failed"]] == ["919999990000"]
    assert "132000" in card["whatsapp"]["failed"][0]["error"]
    assert report["error"] is None


@pytest.mark.parametrize("over, reason", [
    ({"WHATSAPP_ENABLED": False}, "whatsapp_disabled"),
    ({"WHATSAPP_ACCESS_TOKEN": " "}, "whatsapp_credentials_missing"),
    ({"WHATSAPP_PHONE_NUMBER_ID": ""}, "whatsapp_credentials_missing"),
])
def test_whatsapp_stays_off_without_its_switches(channels, monkeypatch, over, reason):
    monkeypatch.setattr(J, "_settings", lambda: _settings(**over))
    report = asyncio.run(J.notify_floor_of_new_job_cards(
        _pool([_user(1, "K", phone="9876543210")]), [39582601]))
    assert report["cards"][0]["whatsapp"]["skipped"] == reason and channels["wa"] == []


def test_no_smtp_host_means_no_email(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings(SMTP_HOST=""))
    report = asyncio.run(J.notify_floor_of_new_job_cards(
        _pool([_user(1, "K", email="k@x.in")]), [39582601]))
    assert report["cards"][0]["email"]["skipped"] == "smtp_not_configured" and channels["mail"] == []


def test_nobody_covering_the_floor_sends_nothing(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings())
    report = asyncio.run(J.notify_floor_of_new_job_cards(
        _pool([_user(2, "W202 Floor", email="w@x.in", warehouses=["W202"])]), [39582601]))
    card = report["cards"][0]
    assert card["recipients"] == [] and channels == {"mail": [], "wa": []}
    assert card["email"]["skipped"] == card["whatsapp"]["skipped"] == "no_floor_manager_for_this_place"


def test_no_floor_manager_users_at_all_is_reported_as_such_and_warned(channels, monkeypatch, caplog):
    monkeypatch.setattr(J, "_settings", lambda: pytest.fail("settings are not needed when nobody is told"))
    with caplog.at_level("WARNING", logger=J.logger.name):
        report = asyncio.run(J.notify_floor_of_new_job_cards(_pool([]), [39582601]))
    assert report["cards"] == [] and report["skipped"] == "no_active_floor_manager_users"
    assert channels == {"mail": [], "wa": []}
    assert any(r.levelname == "WARNING" and "floor_manager" in r.getMessage() for r in caplog.records)


def test_a_mail_server_refusal_is_not_reported_as_sent(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings(WHATSAPP_ENABLED=False))
    monkeypatch.setattr(J.mail_service, "_send", lambda *a, **kw: False)
    report = asyncio.run(J.notify_floor_of_new_job_cards(
        _pool([_user(1, "K", email="k@x.in")]), [39582601]))
    assert report["cards"][0]["email"] == {"to": [], "skipped": "send_failed"}


def test_a_failure_anywhere_is_reported_never_raised(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: _settings())
    report = asyncio.run(J.notify_floor_of_new_job_cards(_Pool(_Conn(boom=True)), [39582601]))
    assert report["error"] == "database went away" and channels == {"mail": [], "wa": []}

    def mail_down(*a, **kw):
        raise OSError("smtp down")

    monkeypatch.setattr(J.mail_service, "_send", mail_down)
    report = asyncio.run(J.notify_floor_of_new_job_cards(
        _pool([_user(1, "K", email="k@x.in")]), [39582601]))
    assert report["cards"][0]["email"]["skipped"] == "error: smtp down" and report["error"] is None


def test_no_job_card_ids_is_a_no_op(channels, monkeypatch):
    monkeypatch.setattr(J, "_settings", lambda: pytest.fail("settings are not needed for no cards"))
    report = asyncio.run(J.notify_floor_of_new_job_cards(_pool([]), []))
    assert report["cards"] == [] and report["job_card_ids"] == [] and channels == {"mail": [], "wa": []}
