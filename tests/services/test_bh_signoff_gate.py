"""The business-head approval belongs at the START of the NPD flow, not the end (086).

Before this, a BH was asked to approve a recipe on the finished dev job card — the
promote raised a REQUESTOR_BH gate next to the inventory manager's. That is far too
late to be a decision: the development work is already done. The approval now sits on
the REQUEST, is asked only when someone else raised it on the BH's behalf, and holds
the request back from the NPD team until it is given.

Guards the invariants that carry that intent (no DB — a fake conn):
  1. the gate is raised ONLY when the sales POC differs from the selected business head;
  2. a BH raising their own request is auto-approved with NO message, and the audit
     still records who it was auto-approved for;
  3. a held request does NOT reach the NPD team — not the alert, not the review card;
  4. approving releases it to NPD; rejecting kills it at BH_REJECTED and NPD never sees it;
  5. only the BOUND business head (or an admin) can clear it;
  6. the dev-JC promote raises the inventory-manager gate ALONE.

Run:  PYTHONPATH=. python -m pytest tests/services/test_bh_signoff_gate.py
"""
from __future__ import annotations

import asyncio
import types

import pytest
from fastapi import HTTPException

from app.modules.sample.services import approval_service as aps
from app.modules.sample.services import promote_approval_service as pas
from app.modules.sample.services import requisition_service as req_svc

BH_UID, POC_UID, NPD_UID = 100, 300, 400

BASE_REQ = {
    "id": 7, "request_id": 88881111, "sample_type": "NPD", "status": "SUBMITTED",
    "requestor_user_id": BH_UID, "business_head_user_id": BH_UID,
    "requestor_team": "Ravi Menon", "created_by": POC_UID,
    "sales_poc_user_id": POC_UID, "sales_poc_name": "Sana Sales",
    "npd_target_name": "Peri Peri Fries", "warehouse": "W202", "quantity": 12,
    "bh_signoff_state": None, "bh_signoff_at": None, "bh_signoff_by": None,
}


def _user(uid, role="sales", admin=False):
    return types.SimpleNamespace(user_id=uid, role_name=role, is_admin=admin,
                                 full_name=f"user{uid}")


class _Txn:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Conn:
    """Records every statement; answers the handful of reads these services issue."""

    def __init__(self, req=None):
        self.req = dict(req or BASE_REQ)
        self.sql: list[tuple[str, tuple]] = []
        self.approvals: list[dict] = []

    def _log(self, sql, p):
        self.sql.append((" ".join(sql.split()), p))

    def transaction(self):
        return _Txn()

    async def execute(self, sql, *p):
        self._log(sql, p)
        flat = " ".join(sql.split())
        if "UPDATE sample_requisitions" in flat and "bh_signoff_state" in flat:
            self.req["bh_signoff_state"] = p[1] if len(p) > 1 else "PENDING"
            if "bh_signoff_state = 'PENDING'" in flat:
                self.req["bh_signoff_state"] = "PENDING"
        if "SET status" in flat or "status = $2" in flat:
            if len(p) > 1 and isinstance(p[1], str):
                self.req["status"] = p[1]

    async def fetchval(self, sql, *p):
        self._log(sql, p)
        flat = " ".join(sql.split())
        if "MAX(sequence_no)" in flat:
            return len(self.approvals) + 1
        return None

    async def fetchrow(self, sql, *p):
        self._log(sql, p)
        flat = " ".join(sql.split())
        if "INSERT INTO sample_approvals" in flat:
            row = {"id": len(self.approvals) + 1, "requisition_id": p[0],
                   "approval_stage": p[1], "approver_user_id": p[2],
                   "role_at_action": p[3], "action": p[4], "remarks": p[5],
                   "sequence_no": p[6]}
            self.approvals.append(row)
            return row
        if "FROM sample_requisitions" in flat:
            return dict(self.req)
        return None

    async def fetch(self, sql, *p):
        self._log(sql, p)
        if "FROM sample_approvals" in " ".join(sql.split()):
            return list(self.approvals)
        return []

    # ── assertions helpers ────────────────────────────────────────────────
    def ran(self, needle: str) -> bool:
        return any(needle in q for q, _ in self.sql)


@pytest.fixture
def wired(monkeypatch):
    """086 migration present; every outbound channel captured instead of sent."""
    calls = {"npd": [], "bh_mail": [], "bh_wa": [], "trail": [], "alert": []}

    async def _true(conn): return True
    monkeypatch.setattr(req_svc, "has_bh_signoff_columns", _true)

    async def _release(conn, req): calls["npd"].append(req.get("id"))
    monkeypatch.setattr(req_svc, "release_to_npd", _release)

    async def _notify_bh(conn, req): calls["bh_mail"].append(req.get("id"))
    monkeypatch.setattr(aps, "notify_bh_signoff", _notify_bh)

    async def _alert(conn, **k): calls["alert"].append(k.get("alert_type"))
    monkeypatch.setattr(aps.notification_service, "emit_alert", _alert)

    async def _audit(conn, *a, **k): return None
    monkeypatch.setattr(aps.audit_service, "write_audit", _audit)

    from app.modules.sample.services import sample_mail_service as mail

    async def _trail(conn, req, *, event, reason=None, **k): calls["trail"].append(event)
    monkeypatch.setattr(mail, "notify_requisition_event", _trail)
    return calls


# ── 1 + 2. when is the gate raised at all ───────────────────────────────────

def test_gate_is_raised_only_when_someone_else_raised_the_request():
    assert aps.bh_signoff_decision(BASE_REQ) == ("PENDING", BH_UID)


def test_a_business_head_raising_their_own_request_is_auto_approved():
    own = {**BASE_REQ, "sales_poc_user_id": BH_UID}
    assert aps.bh_signoff_decision(own) == ("AUTO_APPROVED", BH_UID)


def test_a_free_text_poc_falls_back_to_who_actually_raised_it():
    """sales_poc_user_id is NULL for a POC with no login (or a pre-085 row) — the
    person who raised it is created_by, and that is who the rule must compare."""
    raised_by_bh = {**BASE_REQ, "sales_poc_user_id": None, "created_by": BH_UID}
    assert aps.bh_signoff_decision(raised_by_bh) == ("AUTO_APPROVED", BH_UID)


def test_no_business_head_named_means_nobody_to_ask():
    assert aps.bh_signoff_decision({**BASE_REQ, "business_head_user_id": None}) \
        == ("NOT_REQUIRED", None)


def test_auto_approval_still_records_who_it_was_approved_for(wired):
    """'nobody was asked' and 'nobody approved' have to stay distinguishable later."""
    conn = _Conn({**BASE_REQ, "sales_poc_user_id": BH_UID})
    state = asyncio.run(aps.arm_bh_signoff(conn, dict(conn.req), user=_user(BH_UID)))
    assert state == "AUTO_APPROVED"
    assert len(conn.approvals) == 1
    row = conn.approvals[0]
    assert row["approval_stage"] == "REQUESTOR_BH_SIGNOFF"
    assert row["action"] == "APPROVED"
    # stamped to the BUSINESS HEAD, not to whatever role happened to click submit
    assert row["approver_user_id"] == BH_UID and row["role_at_action"] == "business_head"


def test_a_pending_gate_records_no_approval_yet(wired):
    conn = _Conn()
    assert asyncio.run(aps.arm_bh_signoff(conn, dict(conn.req), user=_user(POC_UID))) == "PENDING"
    assert conn.approvals == []


def test_an_unmigrated_database_keeps_the_old_behaviour(monkeypatch, wired):
    """086 is hand-applied; before it lands, submit must not gate anything."""
    async def _false(conn): return False
    monkeypatch.setattr(req_svc, "has_bh_signoff_columns", _false)
    conn = _Conn()
    assert asyncio.run(aps.arm_bh_signoff(conn, dict(conn.req), user=_user(POC_UID))) == "NOT_REQUIRED"
    assert not conn.ran("bh_signoff_state")


# ── 3. a held request must not reach NPD ────────────────────────────────────

def test_submit_holds_the_request_back_from_npd_until_the_bh_approves(wired):
    conn = _Conn({**BASE_REQ, "status": "DRAFT"})
    asyncio.run(req_svc.submit_requisition(conn, 7, user=_user(POC_UID)))
    assert wired["bh_mail"] == [7], "the business head must be asked"
    assert wired["npd"] == [], "NPD must not be told about an unapproved request"


def test_submit_hands_straight_to_npd_when_the_bh_raised_it(wired):
    conn = _Conn({**BASE_REQ, "status": "DRAFT", "sales_poc_user_id": BH_UID})
    asyncio.run(req_svc.submit_requisition(conn, 7, user=_user(BH_UID, "business_head")))
    assert wired["npd"] == [7]
    assert wired["bh_mail"] == [], "no approval message when the BH raised it themselves"


# ── 4 + 5. acting on the gate ───────────────────────────────────────────────

def test_approving_releases_the_request_to_npd(wired):
    conn = _Conn({**BASE_REQ, "bh_signoff_state": "PENDING"})
    asyncio.run(aps.act_bh_signoff(conn, 7, action="APPROVED", user=_user(BH_UID, "business_head")))
    assert conn.req["bh_signoff_state"] == "APPROVED"
    assert conn.req["status"] == "SUBMITTED", "NPD review still owns the status move"
    assert wired["npd"] == [7]
    assert wired["trail"] == ["bh signed off"]


def test_rejecting_kills_it_before_npd_ever_sees_it(wired):
    conn = _Conn({**BASE_REQ, "bh_signoff_state": "PENDING"})
    asyncio.run(aps.act_bh_signoff(conn, 7, action="REJECTED", user=_user(BH_UID, "business_head"),
                                   remarks="Customer pulled out"))
    assert conn.req["bh_signoff_state"] == "REJECTED"
    assert conn.req["status"] == "BH_REJECTED"
    assert wired["npd"] == [], "a rejected request must never reach NPD"
    assert wired["trail"] == ["rejected"]


def test_a_rejection_must_carry_a_reason(wired):
    conn = _Conn({**BASE_REQ, "bh_signoff_state": "PENDING"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(aps.act_bh_signoff(conn, 7, action="REJECTED", user=_user(BH_UID)))
    assert e.value.detail["error"] == "reason_required"


def test_only_the_bound_business_head_can_clear_the_gate(wired):
    conn = _Conn({**BASE_REQ, "bh_signoff_state": "PENDING"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(aps.act_bh_signoff(conn, 7, action="APPROVED", user=_user(999, "business_head")))
    assert e.value.status_code == 403
    assert e.value.detail["error"] == "not_the_approver"


def test_an_admin_may_clear_it(wired):
    conn = _Conn({**BASE_REQ, "bh_signoff_state": "PENDING"})
    asyncio.run(aps.act_bh_signoff(conn, 7, action="APPROVED", user=_user(999, "admin", admin=True)))
    assert conn.req["bh_signoff_state"] == "APPROVED"


def test_a_stale_second_tap_is_refused_not_double_recorded(wired):
    conn = _Conn({**BASE_REQ, "bh_signoff_state": "APPROVED"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(aps.act_bh_signoff(conn, 7, action="APPROVED", user=_user(BH_UID)))
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "stage_already_actioned"


def test_npd_cannot_review_a_request_whose_bh_has_not_approved(wired):
    conn = _Conn({**BASE_REQ, "bh_signoff_state": "PENDING"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(aps.act_npd_review(conn, 7, action="ACCEPT", user=_user(NPD_UID, "npd_team")))
    assert e.value.status_code == 409
    assert e.value.detail["error"] == "awaiting_bh_signoff"


# ── 6. the promote gate the approval moved OFF ──────────────────────────────

def test_the_promote_raises_the_inventory_gate_alone(monkeypatch):
    """The whole point of 086: no REQUESTOR_BH gate on a finished job card."""
    gates: list[str] = []

    async def _insert(conn, sql, *params):
        flat = " ".join(sql.split())
        if "npd_dev_promote_approval" in flat:
            gates.append("REQUESTOR_BH" if "REQUESTOR_BH" in flat else "INV_MGR")
        return 123

    async def _noop(*a, **k): return None

    monkeypatch.setattr(pas, "_insert_8d", _insert)
    monkeypatch.setattr(pas.notification_service, "emit_alert", _noop)
    from app.modules.sample.services import sample_mail_service as mail
    from app.modules.sample.services import whatsapp_service as wa
    monkeypatch.setattr(mail, "notify_promote_review_email", _noop)
    monkeypatch.setattr(wa, "notify_promote_review", _noop)

    conn = _Conn()
    res = asyncio.run(pas.open_promote_request(conn, 55550000, payload={}, user=_user(NPD_UID)))
    assert gates == ["INV_MGR"]
    assert res["status"] == "PENDING_APPROVAL"
    assert not conn.ran("business_head_user_id"), \
        "the promote no longer needs to resolve a BH — it raises no gate for one"


# ── 7. the BH acting from WhatsApp ──────────────────────────────────────────
# The BH is neither an npd_team reviewer nor a promote approver, so their tap has to be
# resolved BEFORE both of those flows — otherwise it dead-ends in "not recognised as an
# NPD reviewer", which is exactly what the promote gate's own routing had to fix.

@pytest.fixture
def wa_wired(monkeypatch):
    """Inbound plumbing: the tap resolves to a request, the sender to a user, and the
    decision is captured instead of hitting the service."""
    from app.modules.sample.services import whatsapp_service as ws

    box = {"sent": [], "acted": [], "armed": []}

    async def _send_text(to, msg, *a, **k):
        box["sent"].append(msg)
        return {"ok": True}

    async def _none(*a, **k):
        return None

    async def _bh_for_wamid(conn, wamid):
        return 7 if wamid == "wamid.bh.card" else None

    async def _resolve_user(conn, phone):
        return {"user_id": BH_UID, "role_name": "business_head"}

    async def _act(conn, req_id, *, action, user, remarks=None):
        box["acted"].append((req_id, action, remarks))
        return {}

    async def _arm(conn, phone, req_id):
        box["armed"].append(req_id)

    monkeypatch.setattr(ws, "_send_text", _send_text)
    monkeypatch.setattr(ws, "_bh_signoff_req_for_wamid", _bh_for_wamid)
    monkeypatch.setattr(ws, "_resolve_user", _resolve_user)
    monkeypatch.setattr(ws, "_set_pending_bh_reject", _arm)
    monkeypatch.setattr(ws, "_pop_pending_bh_reject", _none)
    monkeypatch.setattr(ws, "_promote_for_wamid", _none)
    monkeypatch.setattr(ws, "_pop_promote_pending", _none)
    monkeypatch.setattr(ws, "_pop_pending", _none)
    monkeypatch.setattr(aps, "act_bh_signoff", _act)

    from app.modules.purchase.services import po_intimation as po
    monkeypatch.setattr(po, "handle_po_intimation_tap", _none)
    from app.modules.customer_returns.services import wa_notify as cr
    monkeypatch.setattr(cr, "handle_return_button_tap", _none)
    return box


def _wa(text, *, context_id=None):
    from app.modules.sample.services import whatsapp_service as ws
    return asyncio.run(ws.handle_inbound(
        _Conn(), from_phone="919876543210", text=text, context_id=context_id))


def test_an_approve_tap_on_the_bh_card_records_the_approval(wa_wired):
    res = _wa("Approve", context_id="wamid.bh.card")
    assert res["ok"] and res["action"] == "APPROVED"
    assert wa_wired["acted"] == [(7, "APPROVED", None)]


def test_a_reject_tap_asks_for_the_reason_before_recording_anything(wa_wired):
    res = _wa("Reject", context_id="wamid.bh.card")
    assert res["awaiting"] == "bh_reject_reason"
    assert wa_wired["armed"] == [7]
    assert wa_wired["acted"] == [], "a rejection must not land without a reason"


def test_free_text_on_the_bh_card_is_not_read_as_an_approval(wa_wired):
    """Answering a question with '✓ Approved' would record a decision nobody made."""
    res = _wa("what is this for?", context_id="wamid.bh.card")
    assert res == {"ok": False, "reason": "unparsed", "requisition_id": 7}
    assert wa_wired["acted"] == []


def test_the_armed_reason_is_what_records_the_rejection(wa_wired, monkeypatch):
    from app.modules.sample.services import whatsapp_service as ws

    async def _pop(conn, phone): return 7
    monkeypatch.setattr(ws, "_pop_pending_bh_reject", _pop)
    res = _wa("Customer pulled out")
    assert res["ok"] and res["action"] == "REJECTED"
    assert wa_wired["acted"] == [(7, "REJECTED", "Customer pulled out")]
