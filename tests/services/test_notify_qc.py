""""Notify QC" emails the QC people covering the card, and says who (agreed
2026-10-24).

Before (audit 2026-10-23): the hook that should have sent anything was a no-op
nothing ever replaced, and the recipient query read the primary role only —
there is no qc_inspector on RDS, only one qc_manager — so nobody was ever told
while the screen showed "N dispatched". Now: QC is qc_inspector or qc_manager
among ALL roles, covering the card's place; each is emailed; when nobody was
reached the answer is a 409 naming why.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.modules.production import router as R
from app.modules.production.services import mail_service, qc_notify, wa_notify

USER = SimpleNamespace(is_admin=False, full_name="Ravi K", email=None, phone="98", user_id=21,
                       allowed_floors=[], allowed_warehouses=[], role_name="floor_manager")
CARD = {"status": "completed", "entity": "cfpl", "factory": "A185", "floor": "Roasting Area",
        "job_card_number": "PLAN-1-L2-S1", "fg_sku_name": "Cashew W320 1kg",
        "customer_name": "Vedaka", "process_name": "Roasting"}


class _Conn:
    def __init__(self, card=CARD):
        self.card = card
        self.logged: list[tuple] = []

    def is_in_transaction(self): return True

    def transaction(self):
        class _Tx:
            async def __aenter__(self): return None
            async def __aexit__(self, *exc): return False
        return _Tx()

    async def fetchrow(self, sql, *args):
        if "INSERT INTO qc_notification_log_v2" in sql:
            self.logged.append((args[2], args[4], args[6]))   # recipient, channel, status
            return {"notification_id": 900 + len(self.logged)}
        return self.card


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self): return conn
            async def __aexit__(self, *exc): return False
        return _Acq()


def _qc(uid, name, email, floors=("Roasting Area",)):
    return {"user_id": uid, "name": name, "email": email, "phone": "",
            "allowed_warehouses": ["A185"], "allowed_floors": list(floors)}


@pytest.fixture
def sent(monkeypatch):
    mails = []

    def send(subject, body, to, cc, **kw):
        mails.append((subject, body, to))
        return True
    monkeypatch.setattr(mail_service, "_send", send)
    monkeypatch.setattr(qc_notify, "current_settings",
                        lambda: SimpleNamespace(SMTP_HOST="smtp.x", WEB_APP_URL="https://erpcf.in"))
    return mails


def _holders(monkeypatch, people):
    seen = {}

    async def holders(conn, roles):
        seen["roles"] = tuple(roles)
        return people
    monkeypatch.setattr(wa_notify, "role_holders", holders)
    return seen


def _notify(conn, note=None):
    body = R.NotifyQCRequest(note=note)
    return asyncio.run(R.notify_qc_v2(SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(db_pool=_Pool(conn)))), 41, body, user=USER))


def test_the_qc_manager_is_emailed_and_named(monkeypatch, sent):
    seen = _holders(monkeypatch, [_qc(7, "Priya S", "priya@candorfoods.in")])
    conn = _Conn()
    out = _notify(conn, note="Check moisture")
    assert seen["roles"] == ("qc_inspector", "qc_manager")
    assert out["dispatched"] == 1 and out["message"] == "Emailed Priya S."
    subject, body, to = sent[0]
    assert to == ["priya@candorfoods.in"]
    assert subject == "QC check needed: job card PLAN-1-L2-S1"
    assert "Product: Cashew W320 1kg" in body and "Place: A185 · Roasting Area" in body
    assert "Asked by: Ravi K" in body and "Note: Check moisture" in body
    assert "https://erpcf.in/modules/job-card/41" in body
    assert conn.logged == [(7, "email", "delivered")]


def test_qc_on_another_floor_is_not_emailed(monkeypatch, sent):
    _holders(monkeypatch, [_qc(7, "Priya S", "priya@candorfoods.in", floors=("Terrace",))])
    conn = _Conn()
    with pytest.raises(HTTPException) as exc:
        _notify(conn)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "qc_not_reached"
    assert "no QC inspector or QC manager covers A185 · Roasting Area" in exc.value.detail["message"]
    assert sent == []
    assert conn.logged == [(None, "email", "failed")]          # the attempt is still logged


def test_qc_with_no_email_is_an_error_not_a_success(monkeypatch, sent):
    _holders(monkeypatch, [_qc(7, "Priya S", "")])
    with pytest.raises(HTTPException) as exc:
        _notify(_Conn())
    assert exc.value.detail["message"] == "Nobody was told: Priya S has no email on file."


def test_email_switched_off_is_said(monkeypatch):
    _holders(monkeypatch, [_qc(7, "Priya S", "priya@candorfoods.in")])
    monkeypatch.setattr(qc_notify, "current_settings",
                        lambda: SimpleNamespace(SMTP_HOST="", WEB_APP_URL=""))
    with pytest.raises(HTTPException) as exc:
        _notify(_Conn())
    assert exc.value.detail["message"] == "Nobody was told: email is switched off on this server."


def test_some_reached_some_not(monkeypatch, sent):
    _holders(monkeypatch, [_qc(7, "Priya S", "priya@candorfoods.in"), _qc(8, "Amit D", "")])
    out = _notify(_Conn())
    assert out["message"] == "Emailed Priya S. Not reached: Amit D has no email on file."


def test_only_completed_cards(monkeypatch, sent):
    _holders(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        _notify(_Conn({**CARD, "status": "in_progress"}))
    assert exc.value.detail["error"] == "invalid_status"


def test_the_default_hook_sends_email():
    assert qc_notify.get_qc_notify_hook() is qc_notify.email_hook
