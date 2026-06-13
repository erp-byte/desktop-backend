"""Threaded review-email flow (rollback-verified).

REQUIRES: DATABASE_URL in the env + migration 071_requisition_email_thread.sql
applied (adds email_thread_msgid / last_reminder_at / reminder_count). Run by a
human — this harness opens a real connection and rolls everything back.

SMTP is never touched: sample_mail_service._send is monkeypatched to a capturing
stub that records every (subject, to, in_reply_to, html) call and returns a fake
Message-ID (so the thread-anchor bookkeeping still exercises). Within one rolled-
back transaction we:
  - seed an NPD requisition (known request_id) + an npd_team auth_user with an email
  - notify_npd_review_email → assert the anchor msgid is stored on the row AND the
    captured Accept URL carries the request_id + the reviewer email
  - notify_requestor_email → assert the captured in_reply_to == the stored anchor
  - send_due_reminders twice → assert the 2nd call sends 0 (cadence guard)
Rolls back so nothing persists."""
import asyncio
import os
import types

import asyncpg
from dotenv import load_dotenv

from app.modules.sample.services import requisition_service as rsvc
from app.modules.sample.services import sample_mail_service as mail

load_dotenv()

TEST_EMAIL = "npd.reviewer.test@candorfoods.in"

# Columns added by migrations 064/066/071 — added IF NOT EXISTS so the harness
# runs even on a DB where 071 hasn't been applied yet (still in-txn, rolled back).
_064 = ("company_name TEXT", "customer_name TEXT", "customer_contact TEXT",
        "customer_ship_to_address TEXT", "mode_of_transport TEXT",
        "expected_dispatch_date DATE", "confirmed_dispatch_date DATE",
        "pcs NUMERIC(15,3)", "weight_per_piece NUMERIC(15,4)")
_071 = ("email_thread_msgid TEXT", "last_reminder_at TIMESTAMPTZ",
        "reminder_count INT NOT NULL DEFAULT 0")


async def _mk_submitted(c, user, target):
    payload = {"sample_type": "NPD", "warehouse": "W202", "npd_target_name": target,
               "pcs": 10, "weight_per_piece": 0.3, "requestor_team": "Sales",
               "description": "email flow test", "articles": []}
    req = await rsvc.create_requisition(c, payload=payload, user=user)
    await rsvc.submit_requisition(c, req["id"], user=user)
    return req


async def main():
    captured = []  # list of dicts: subject / to / in_reply_to / html

    def _stub(subject, html, to_addrs, *, cc=None, msgid=None, in_reply_to=None):
        mid = msgid or f"<stub-{len(captured)}@candorfoods.in>"
        captured.append({"subject": subject, "to": list(to_addrs), "html": html,
                         "in_reply_to": in_reply_to, "msgid": mid})
        return mid

    mail._send = _stub  # monkeypatch — no real SMTP

    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    pre = await c.fetchval("SELECT COUNT(*) FROM sample_requisitions")
    fails, checks = [], {}
    tr = c.transaction()
    await tr.start()
    try:
        for col in (*_064, *_071):
            await c.execute(f"ALTER TABLE sample_requisitions ADD COLUMN IF NOT EXISTS {col}")

        # An npd_team reviewer with a known email so recipients resolve.
        role_id = await c.fetchval("SELECT role_id FROM auth_role WHERE role_name = 'npd_team'")
        uid = await c.fetchval("SELECT user_id FROM auth_user ORDER BY user_id LIMIT 1")
        await c.execute("UPDATE auth_user SET email = $2, role_id = $3 WHERE user_id = $1",
                        uid, TEST_EMAIL, role_id)
        user = types.SimpleNamespace(user_id=uid, role_name="npd_team", is_admin=False, full_name="R")

        recips = await mail._emails_for_role(c, "npd_team")
        checks["DB resolves npd_team email as recipient"] = TEST_EMAIL in recips

        # ── review email: anchor stored + Accept URL carries id + email ──
        captured.clear()
        req = await _mk_submitted(c, user, "Email Review Target")
        fresh = await rsvc.get_requisition(c, req["id"])
        await mail.notify_npd_review_email(c, fresh)
        anchor = await c.fetchval(
            "SELECT email_thread_msgid FROM sample_requisitions WHERE id = $1", req["id"])
        review_mails = [m for m in captured if str(req["request_id"]) in (m["subject"] or "")]
        checks["anchor email_thread_msgid stored on row"] = bool(anchor) and anchor == review_mails[0]["msgid"]
        accept_html = review_mails[0]["html"] if review_mails else ""
        checks["Accept URL carries request_id + reviewer email"] = (
            f"request_id={req['request_id']}" in accept_html
            and "npd-accept" in accept_html
            and "npd.reviewer.test%40candorfoods.in" in accept_html)

        # ── outcome reply threads onto the stored anchor ──
        captured.clear()
        await mail.notify_requestor_email(c, fresh, action="ACCEPT", reason=None)
        reply = next((m for m in captured if "ACCEPTED" in (m["subject"] or "")), None)
        checks["requestor reply in_reply_to == anchor"] = reply is not None and reply["in_reply_to"] == anchor

        # ── reminder cadence guard: first sweep nudges, second is a no-op ──
        captured.clear()
        n1 = await mail.send_due_reminders(c)
        n2 = await mail.send_due_reminders(c)
        rc = await c.fetchval("SELECT reminder_count FROM sample_requisitions WHERE id = $1", req["id"])
        checks["reminder sweep #1 nudged at least this req"] = n1 >= 1
        checks["reminder sweep #2 sends 0 (cadence guard)"] = n2 == 0
        checks["reminder_count incremented once"] = rc == 1

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
    passed = len(checks) - len(fails)
    if fails:
        print("FAIL:", fails)
        print(f"{passed}/{len(checks)} PASS")
        raise SystemExit(1)
    print(f"{len(checks)}/{len(checks)} PASS")


asyncio.run(main())
