"""job_card_unlock_notify: when a job card is unlocked, the notice is about THAT
card (the one that can now start), goes to that card's own floor manager and
team leader, and nothing in it can fail the request that unlocked the card.
No database, SMTP server or Graph API is touched."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.modules.production.services import job_card_notify as J
from app.modules.production.services import job_card_unlock_notify as U

NOW = datetime(2026, 10, 6, 5, 12, tzinfo=timezone.utc)          # 10:42 IST

UPSTREAM = {   # the card whose batch closed / material was dispatched
    "job_card_id": 7001, "job_card_number": "PLAN-145504-L145549-S1",
    "fg_sku_name": "Roasted Almonds 200g", "customer_name": "DMart",
    "step_number": 1, "process_name": "Sorting", "factory": "A-185", "floor": "Sorting Area",
    "status": "in_progress", "prev_job_card_id": None, "assigned_to_team_leader": "Suresh T",
    "plan_id": 145504,
}
DOWNSTREAM = {  # the card that was waiting and is now unlocked
    **UPSTREAM, "job_card_id": 7002, "job_card_number": "PLAN-145504-L145549-S2",
    "step_number": 2, "process_name": "Roasting", "floor": "Roasting Area",
    "status": "unlocked", "prev_job_card_id": 7001, "assigned_to_team_leader": "Meena S",
}
NO_FLOOR = {**DOWNSTREAM, "job_card_id": 7003, "step_number": 2, "process_name": "Packing",
            "floor": None, "assigned_to_team_leader": None}


def _fm(uid, name, phone, floors, email=""):
    """A floor manager as RECIPIENTS_SQL returns one."""
    return {"user_id": uid, "full_name": name, "email": email, "phone": phone,
            "allowed_warehouses": ["A185"], "allowed_floors": floors}


FLOOR_MANAGERS = [
    _fm(1, "Kaushal Patil", "9876543210", ["Roasting Area"], email="k@candorfoods.in"),
    _fm(2, "Sorting Manager", "9123456789", ["Sorting Area"], email="s@candorfoods.in"),
]
ALL_USERS = FLOOR_MANAGERS + [
    {"user_id": 11, "full_name": "Meena S", "email": "m@candorfoods.in", "phone": "9000000011",
     "allowed_warehouses": [], "allowed_floors": []},
    {"user_id": 12, "full_name": "Suresh T", "email": "", "phone": "9000000012",
     "allowed_warehouses": [], "allowed_floors": []},
    {"user_id": 13, "full_name": "Priya P", "email": "p@candorfoods.in", "phone": "9000000013",
     "allowed_warehouses": [], "allowed_floors": []},
    {"user_id": 21, "full_name": "Ravi K", "email": "", "phone": "9000000021",
     "allowed_warehouses": [], "allowed_floors": []},
    {"user_id": 22, "full_name": "Ravi K", "email": "", "phone": "9000000022",
     "allowed_warehouses": [], "allowed_floors": []},
]
PLANS = [{"plan_id": 145504, "created_by": "Priya P"}]


class _Conn:
    def __init__(self, cards=(UPSTREAM, DOWNSTREAM, NO_FLOOR), boom=False, floor_managers=None):
        self.cards, self.boom = list(cards), boom
        self.floor_managers = FLOOR_MANAGERS if floor_managers is None else floor_managers

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if self.boom:
            raise RuntimeError("database went away")
        if "FROM auth_user u" in s and "auth_user_role" in s:
            return self.floor_managers
        if "FROM auth_user u" in s:
            names = set(args[0])
            return [u for u in ALL_USERS if u["full_name"].strip().lower() in names]
        if "FROM job_card_v2" in s:
            return [c for c in self.cards if c["job_card_id"] in args[0]]
        if "FROM production_plan_v2" in s:
            return [p for p in PLANS if p["plan_id"] in args[0]]
        raise AssertionError(f"unexpected fetch: {s[:80]}")


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


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
        sent["mail"].append({"subject": subject, "body": body, "to": to, **kw})
        return True

    async def fake_wa(settings, message):
        if message["to"].endswith("0000"):
            raise RuntimeError("HTTP 400 - (#132000) template param count mismatch")
        sent["wa"].append(message)

    monkeypatch.setattr(U.mail_service, "_send", fake_send)
    monkeypatch.setattr(U, "_send_whatsapp_template", fake_wa)
    monkeypatch.setattr(U, "_settings", lambda: _settings())
    return sent


def _run(unlocks, conn=None, **kw):
    kw.setdefault("unlocked_by", "Ravi K")
    return asyncio.run(U.notify_floor_of_unlocked_job_cards(_Pool(conn or _Conn()), unlocks, now=NOW, **kw))


def _body(message):
    return [p["text"] for p in message["template"]["components"][1]["parameters"]]


def _header(message):
    return message["template"]["components"][0]["parameters"][0]["text"]


DISPATCHED = {"job_card_id": 7002, "from_job_card_id": 7001, "qty_kg": 149.8, "how": "dispatch"}


# ── which card ──────────────────────────────────────────────────────────────

def test_the_notice_is_about_the_unlocked_card_not_the_one_that_dispatched(channels):
    _run([DISPATCHED])
    msg = channels["wa"][0]
    assert _header(msg) == "7002"
    assert _body(msg) == [
        "7002",                          # the unlocked card
        "Roasted Almonds 200g",
        "DMart",
        "2 · Roasting",                  # ITS step, not the dispatching card's Sorting
        "A-185 · Roasting Area",         # ITS place
        "149.800 kg",
        "7001 · Sorting",                # where the material came from
        "Ravi K on 06 Oct 2026",
        "10:42",
        "-",
    ]


# ── who ─────────────────────────────────────────────────────────────────────

def test_the_unlocked_cards_floor_and_team_leader_are_told_not_the_dispatching_floor(channels):
    report = _run([DISPATCHED])
    (entry,) = report["cards"]
    assert entry["job_card_id"] == 7002 and entry["from_job_card_id"] == 7001
    assert entry["recipients"] == ["Kaushal Patil", "Meena S"]
    assert [m["to"] for m in channels["wa"]] == ["919876543210", "919000000011"]
    assert channels["mail"][0]["to"] == ["k@candorfoods.in", "m@candorfoods.in"]


def test_a_team_leader_name_is_matched_ignoring_case_and_spaces(channels):
    conn = _Conn(cards=(UPSTREAM, {**DOWNSTREAM, "assigned_to_team_leader": "  meena s "}))
    (entry,) = _run([DISPATCHED], conn=conn)["cards"]
    assert "Meena S" in entry["recipients"]


@pytest.mark.parametrize("leader", ["Ravi K", "Nobody Here", None, ""])
def test_a_team_leader_name_that_is_not_exactly_one_user_is_skipped(channels, leader):
    conn = _Conn(cards=(UPSTREAM, {**DOWNSTREAM, "assigned_to_team_leader": leader}))
    (entry,) = _run([DISPATCHED], conn=conn)["cards"]
    assert entry["recipients"] == ["Kaushal Patil"]          # two "Ravi K" accounts: neither is guessed


def test_whoever_unlocked_it_is_not_told_about_it(channels):
    (entry,) = _run([DISPATCHED], actor_user_id=1)["cards"]
    assert entry["recipients"] == ["Meena S"]


def test_a_card_with_no_floor_goes_to_the_previous_floor_and_the_planner(channels):
    (entry,) = _run([{"job_card_id": 7003, "from_job_card_id": 7001, "qty_kg": 50, "how": "dispatch"}])["cards"]
    assert entry["recipients"] == ["Sorting Manager", "Priya P"]
    body = _body(channels["wa"][0])
    assert body[4] == "A-185 · floor not set"
    assert body[9] == "Floor not set on this card - please assign one"


def test_a_force_unlock_says_why_and_claims_no_material(channels):
    _run([{"job_card_id": 7002, "how": "force_unlock", "reason": "QC cleared the lot by hand"}])
    body = _body(channels["wa"][0])
    assert body[5] == "-" and body[6] == "-"
    assert body[9] == "Force-unlocked: QC cleared the lot by hand"


def test_the_same_card_unlocked_twice_in_one_request_is_told_once(channels):
    report = _run([DISPATCHED, {**DISPATCHED, "qty_kg": 1.0}, {"job_card_id": "x"}, {"job_card_id": None}])
    assert [c["job_card_id"] for c in report["cards"]] == [7002]
    assert len(channels["wa"]) == 2


# ── the template ────────────────────────────────────────────────────────────

def test_template_message_matches_the_template(channels):
    _run([DISPATCHED])
    msg = channels["wa"][0]
    assert msg["messaging_product"] == "whatsapp" and msg["type"] == "template"
    assert msg["template"]["name"] == "job_card_unlocked_floor"
    assert msg["template"]["language"] == {"code": "en"}
    assert [c["type"] for c in msg["template"]["components"]] == ["header", "body"]
    assert len(_body(msg)) == len(re.findall(r"\{\{\d+\}\}", U.BODY_TEXT)) == 10


def test_the_open_button_is_sent_only_when_the_template_has_one(channels, monkeypatch):
    monkeypatch.setattr(U, "OPEN_BUTTON", True)
    _run([DISPATCHED])
    button = channels["wa"][0]["template"]["components"][2]
    assert button == {"type": "button", "sub_type": "url", "index": "0",
                      "parameters": [{"type": "text", "text": "7002"}]}


def test_the_body_neither_starts_nor_ends_on_a_variable():
    """Meta rejects a template whose body begins or ends with a parameter."""
    text = U.BODY_TEXT.strip()
    assert not text.startswith("{{") and not text.endswith("}}")


def test_values_stay_inside_metas_limits_and_are_never_blank_or_multiline():
    huge = "x\n" * 900
    card = {**DOWNSTREAM, "fg_sku_name": huge, "customer_name": huge, "process_name": huge, "floor": huge}
    facts = U.facts(card, {**UPSTREAM, "process_name": huge}, {**DISPATCHED, "reason": huge},
                    unlocked_by=huge, when=NOW)
    msg = U.template_message("919876543210", facts)
    params = _body(msg)
    assert all(p and "\n" not in p and "\t" not in p for p in params)
    assert len(U.HEADER_TEXT.replace("{{1}}", _header(msg))) <= 60
    rendered = U.BODY_TEXT
    for i, p in enumerate(params, start=1):
        rendered = rendered.replace(f"{{{{{i}}}}}", p)
    assert len(rendered) <= 1024


def test_the_unlock_notice_shares_the_creation_notices_plumbing():
    assert U._send_whatsapp_template is J._send_whatsapp_template
    assert U.floor_role_holders is J.floor_role_holders


# ── email ───────────────────────────────────────────────────────────────────

def test_the_email_names_the_unlocked_card_where_it_is_and_links_to_it(channels):
    _run([DISPATCHED])
    mail = channels["mail"][0]
    assert mail["subject"] == "Job card 7002 unlocked - Roasting at A-185 · Roasting Area"
    assert "Material received : 149.800 kg from 7001 · Sorting" in mail["body"]
    assert "https://erpcf.in/modules/job-card/7002" in mail["body"]
    assert mail["entity_id"] == "7002" and mail["event"] == "unlocked"


# ── never fails the request ─────────────────────────────────────────────────

def test_a_graph_error_is_recorded_not_raised(channels):
    refused = [{**FLOOR_MANAGERS[0], "phone": "9876540000"}]    # the fake Graph API refuses ...0000
    (entry,) = _run([DISPATCHED], conn=_Conn(floor_managers=refused))["cards"]
    assert entry["whatsapp"]["sent"] == 1                        # the team leader still gets theirs
    assert entry["whatsapp"]["failed"][0]["phone"] == "919876540000"


def test_whatsapp_stays_off_without_its_switch_but_email_still_goes(channels, monkeypatch):
    monkeypatch.setattr(U, "_settings", lambda: _settings(WHATSAPP_ENABLED=False))
    (entry,) = _run([DISPATCHED])["cards"]
    assert entry["whatsapp"]["skipped"] == "whatsapp_disabled" and channels["wa"] == []
    assert channels["mail"]


def test_a_failure_anywhere_is_reported_never_raised(channels):
    report = _run([DISPATCHED], conn=_Conn(boom=True))
    assert report["error"] == "database went away" and report["cards"] == []


def test_a_card_that_is_no_longer_there_is_reported_not_sent(channels):
    report = _run([{"job_card_id": 9999, "how": "dispatch"}])
    assert report["missing"] == [9999] and channels["wa"] == []


def test_no_unlocks_is_a_no_op(channels, monkeypatch):
    monkeypatch.setattr(U, "_settings", lambda: pytest.fail("settings are not needed for no cards"))
    assert _run([])["cards"] == []
