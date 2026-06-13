"""Inbound-WhatsApp NPD review flow (rollback-verified).

WhatsApp sending is force-disabled (log-only), so no real messages are sent.
Adds the 064/065/067 schema in-txn, makes one auth_user an NPD reviewer with a
test phone, then drives whatsapp_service:
  TEXT COMMANDS
  - HOLD <req#>            → arms a pending hold + asks for the reason
  - free-text reply        → captured as the hold reason → req goes ON_HOLD
  - HOLD <req#> : reason   → inline-reason hold
  - ACCEPT <req#>          → req goes BH_APPROVED
  BUTTON TAPS (resolved via context_id → wa_review_message)
  - "Accept" + context_id  → BH_APPROVED
  - "Hold" + context_id    → arms pending → next reply = reason → ON_HOLD
  PARAM BUILDERS / WEBHOOK PARSING
  - _review_params / _updated_params shapes + the {{2}} difference
  - extract_messages pulls context_id from a button webhook payload
  - notify_npd_updated runs clean (log-only)
  - unknown number rejected
Rolls back so nothing persists."""
import asyncio
import os
import types

os.environ["WHATSAPP_ENABLED"] = "false"   # log-only — never hits the network

import asyncpg
from dotenv import load_dotenv

from app.modules.auth.services.phone import normalize as normalize_phone
from app.modules.sample.services import requisition_service as rsvc
from app.modules.sample.services import whatsapp_service as wa

load_dotenv()

TEST_PHONE = "+919999000123"

_064 = ("company_name TEXT", "customer_name TEXT", "customer_contact TEXT",
        "customer_ship_to_address TEXT", "mode_of_transport TEXT",
        "expected_dispatch_date DATE", "confirmed_dispatch_date DATE",
        "pcs NUMERIC(15,3)", "weight_per_piece NUMERIC(15,4)")


async def _mk_submitted(c, user, target, **extra):
    payload = {"sample_type": "NPD", "warehouse": "W202", "npd_target_name": target,
               "pcs": 10, "weight_per_piece": 0.3, "company_name": "Dmart",
               "customer_name": "Kaushal", "customer_contact": "9004464207",
               "mode_of_transport": "DTDC", "purpose_note": "CUSTOMER DISPLAY",
               "description": "Add new flavour of pudina", "articles": []}
    payload.update(extra)
    req = await rsvc.create_requisition(c, payload=payload, user=user)
    await rsvc.submit_requisition(c, req["id"], user=user)
    return req


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    pre = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        for col in _064:
            await c.execute(f"ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS {col}")
        await c.execute(
            """CREATE TABLE IF NOT EXISTS wa_pending_action (
                 wa_phone TEXT PRIMARY KEY,
                 requisition_id INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
                 action TEXT NOT NULL DEFAULT 'HOLD', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        await c.execute(
            """CREATE TABLE IF NOT EXISTS wa_review_message (
                 wamid TEXT PRIMARY KEY,
                 requisition_id INT NOT NULL REFERENCES sample_requisitions(id) ON DELETE CASCADE,
                 kind TEXT NOT NULL DEFAULT 'REVIEW', wa_phone TEXT,
                 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")

        # Make user uid an npd_team reviewer with the test phone (recipients are
        # now resolved from the DB by role, so the role must be npd_team).
        role_id = await c.fetchval("SELECT role_id FROM auth_role WHERE role_name = 'npd_team'")
        uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
        await c.execute("UPDATE auth_user SET phone = $2, role_id = $3 WHERE user_id = $1",
                        uid, normalize_phone(TEST_PHONE), role_id)
        user = types.SimpleNamespace(user_id=uid, role_name="npd_team", is_admin=False, full_name="R")

        # Recipients come from the DB: an npd_team member with a phone is included.
        recips = await wa._resolve_recipients(c)
        checks["DB resolves npd_team phone as recipient"] = wa._fmt_phone(TEST_PHONE) in recips

        # ── HOLD flow (text command) ──
        r1 = await _mk_submitted(c, user, "WA Hold Target")
        no1 = str(r1["request_id"])
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text=f"HOLD {no1}")
        pend = await c.fetchval("SELECT requisition_id FROM wa_pending_action WHERE wa_phone = $1",
                                wa._fmt_phone(TEST_PHONE))
        checks["HOLD arms pending + asks reason"] = out.get("awaiting") == "reason" and pend == r1["id"]

        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text="Customer changed the spec")
        row = await c.fetchrow("SELECT status FROM sample_requisitions WHERE id = $1", r1["id"])
        remark = await c.fetchval(
            """SELECT remarks FROM sample_approvals WHERE requisition_id = $1 AND action = 'HOLD'
               ORDER BY sequence_no DESC LIMIT 1""", r1["id"])
        gone = await c.fetchval("SELECT COUNT(*) FROM wa_pending_action WHERE wa_phone = $1",
                                wa._fmt_phone(TEST_PHONE))
        checks["reply captured as hold reason → ON_HOLD"] = (
            out.get("ok") and row["status"] == "ON_HOLD"
            and remark == "Customer changed the spec" and gone == 0)

        # ── inline-reason HOLD (text command) ──
        r2 = await _mk_submitted(c, user, "WA Inline Hold")
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text=f"HOLD {r2['request_id']} : pending lab result")
        st2 = await c.fetchval("SELECT status FROM sample_requisitions WHERE id = $1", r2["id"])
        rem2 = await c.fetchval(
            """SELECT remarks FROM sample_approvals WHERE requisition_id = $1 AND action = 'HOLD'
               ORDER BY sequence_no DESC LIMIT 1""", r2["id"])
        checks["inline HOLD reason"] = st2 == "ON_HOLD" and rem2 == "pending lab result"

        # ── ACCEPT flow (text command) ──
        r3 = await _mk_submitted(c, user, "WA Accept Target")
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text=f"ACCEPT {r3['request_id']}")
        st3 = await c.fetchval("SELECT status FROM sample_requisitions WHERE id = $1", r3["id"])
        checks["ACCEPT → BH_APPROVED"] = out.get("ok") and st3 == "BH_APPROVED"

        # ── ACCEPT via BUTTON (bare "Accept" + context_id → wa_review_message) ──
        r4 = await _mk_submitted(c, user, "WA Button Accept")
        wamid4 = "wamid.BUTTON_ACCEPT_4"
        await wa._store_review_message(c, wamid4, r4["id"], "REVIEW", wa._fmt_phone(TEST_PHONE))
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text="Accept", context_id=wamid4)
        st4 = await c.fetchval("SELECT status FROM sample_requisitions WHERE id = $1", r4["id"])
        checks["Accept button → BH_APPROVED"] = out.get("ok") and st4 == "BH_APPROVED"

        # ── HOLD via BUTTON → arms pending → next reply = reason → ON_HOLD ──
        r5 = await _mk_submitted(c, user, "WA Button Hold")
        wamid5 = "wamid.BUTTON_HOLD_5"
        await wa._store_review_message(c, wamid5, r5["id"], "UPDATED", wa._fmt_phone(TEST_PHONE))
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text="Hold", context_id=wamid5)
        pend5 = await c.fetchval("SELECT requisition_id FROM wa_pending_action WHERE wa_phone = $1",
                                 wa._fmt_phone(TEST_PHONE))
        armed = out.get("awaiting") == "reason" and pend5 == r5["id"]
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text="Awaiting customer sign-off")
        st5 = await c.fetchval("SELECT status FROM sample_requisitions WHERE id = $1", r5["id"])
        rem5 = await c.fetchval(
            """SELECT remarks FROM sample_approvals WHERE requisition_id = $1 AND action = 'HOLD'
               ORDER BY sequence_no DESC LIMIT 1""", r5["id"])
        checks["Hold button → reason → ON_HOLD"] = armed and st5 == "ON_HOLD" and rem5 == "Awaiting customer sign-off"

        # ── stale Hold prompt must NOT swallow a later Accept button tap ──
        rA = await _mk_submitted(c, user, "WA Stale Hold A")
        rB = await _mk_submitted(c, user, "WA Stale Accept B")
        await wa._store_review_message(c, "wamid.STALE_A", rA["id"], "REVIEW", wa._fmt_phone(TEST_PHONE))
        await wa._store_review_message(c, "wamid.STALE_B", rB["id"], "REVIEW", wa._fmt_phone(TEST_PHONE))
        await wa.handle_inbound(c, from_phone=TEST_PHONE, text="Hold", context_id="wamid.STALE_A")  # arm pending on A
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text="Accept", context_id="wamid.STALE_B")  # NEW action
        stA = await c.fetchval("SELECT status FROM sample_requisitions WHERE id = $1", rA["id"])
        stB = await c.fetchval("SELECT status FROM sample_requisitions WHERE id = $1", rB["id"])
        pend_left = await c.fetchval("SELECT COUNT(*) FROM wa_pending_action WHERE wa_phone = $1",
                                     wa._fmt_phone(TEST_PHONE))
        checks["Accept tap not swallowed as hold reason"] = (
            out.get("ok") and out.get("action") == "APPROVE"
            and stB == "BH_APPROVED" and stA == "SUBMITTED" and pend_left == 0)

        # ── bare button with NO known context → friendly not_found (no crash) ──
        out = await wa.handle_inbound(c, from_phone=TEST_PHONE, text="Accept", context_id="wamid.UNKNOWN")
        checks["unknown button context → not_found"] = out.get("ok") is False and out.get("reason") == "not_found"

        # ── param builders: 1 header + 14 body; {{2}} differs (req-no vs type) ──
        fresh = await rsvc.get_requisition(c, r4["id"])
        rh, rb = wa._review_params(fresh)
        uh, ub = wa._updated_params(fresh)
        checks["review params shape (1 header, 14 body)"] = len(rh) == 1 and len(rb) == 14
        checks["updated params shape (1 header, 14 body)"] = len(uh) == 1 and len(ub) == 14
        # body[0]: review = request no; updated = type label. body shares company onward.
        checks["review body[0]=req-no, updated body[0]=type"] = (
            rb[0] == wa._req_no(fresh) and ub[0] == "NPD" and rb[1] == ub[1] == "Dmart")
        # numeric tidy + customer values land in the right slots.
        checks["param values mapped correctly"] = (
            rb[2] == "Kaushal" and rb[3] == "9004464207" and rb[5] == "10"
            and rb[6] == "0.3" and rb[7] == "3" and rb[8] == "W202")

        # ── extract_messages pulls context_id from a button webhook payload ──
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "919999000123", "type": "button",
             "button": {"text": "Hold", "payload": "Hold"},
             "context": {"id": "wamid.CTX_99"}}]}}]}]}
        msgs = wa.extract_messages(payload)
        checks["extract_messages → text+context_id"] = (
            len(msgs) == 1 and msgs[0]["text"] == "Hold"
            and msgs[0]["context_id"] == "wamid.CTX_99")

        # ── notify_npd_updated runs clean (log-only, no network) ──
        await wa.notify_npd_updated(c, fresh)
        checks["notify_npd_updated no-throw"] = True

        for k, v in checks.items():
            if not v:
                fails.append(k)
        print("checks:", {k: ("ok" if v else "FAIL") for k, v in checks.items()})
    finally:
        await tr.rollback()

    post = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    if post != pre:
        fails.append(f"LEAK {pre}->{post}")
    await c.close()
    print(f"count restored: {pre} == {post}")
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)
    print("PASS")


asyncio.run(main())
