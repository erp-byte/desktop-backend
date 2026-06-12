"""Read-only: list the WABA's NPD message templates and their HEADER/BODY
placeholder counts + buttons, so we can confirm the live templates match what the
send code in whatsapp_service.py supplies. NO messages are sent (GET only).

    PYTHONPATH=. python scratch/_wa_template_probe.py

Needs WHATSAPP_ACCESS_TOKEN; WABA id defaults to the known Candor WABA but can be
overridden with WHATSAPP_BUSINESS_ACCOUNT_ID.

Expected (must match _review_params / _updated_params):
  npd_request_review   → HEADER 1 placeholder · BODY 14 placeholders · buttons Accept,Hold
  npd_request_updated  → HEADER 1 placeholder · BODY 14 placeholders · buttons Accept,Hold
  npd_request_accepted → HEADER 0 · BODY 3 placeholders
  npd_request_on_hold  → HEADER 0 · BODY 3 placeholders
"""
import asyncio
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

WABA = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "918757177423897")
BASE = os.environ.get("WHATSAPP_GRAPH_BASE", "https://graph.facebook.com/v21.0")
TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
WANT = {
    os.environ.get("WHATSAPP_TPL_NPD_REVIEW", "npd_request_review"),
    os.environ.get("WHATSAPP_TPL_NPD_UPDATED", "npd_request_updated"),
    os.environ.get("WHATSAPP_TPL_NPD_ACCEPTED", "npd_request_accepted"),
    os.environ.get("WHATSAPP_TPL_NPD_HOLD", "npd_request_on_hold"),
}


def _count(comp: dict) -> int:
    return len(set(re.findall(r"{{\s*(\d+)\s*}}", comp.get("text", "") or "")))


async def main():
    if not TOKEN:
        print("WHATSAPP_ACCESS_TOKEN is not set — cannot query templates.")
        return
    url = f"{BASE}/{WABA}/message_templates"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, params={"fields": "name,status,category,language,components",
                                     "limit": 250},
                        headers={"Authorization": f"Bearer {TOKEN}"})
    if r.status_code >= 400:
        print("ERROR", r.status_code, r.text[:600])
        return
    data = r.json().get("data", [])
    for t in data:
        if t["name"] not in WANT:
            continue
        print(f"\n=== {t['name']}  [{t.get('status')} / {t.get('category')} / {t.get('language')}] ===")
        for comp in t.get("components", []):
            ctype = comp.get("type")
            if ctype in ("HEADER", "BODY"):
                print(f"  {ctype}: {_count(comp)} placeholder(s)  (format={comp.get('format', 'TEXT')})")
            elif ctype == "BUTTONS":
                print(f"  BUTTONS: {[b.get('text') for b in comp.get('buttons', [])]}")
            elif ctype == "FOOTER":
                print(f"  FOOTER: {comp.get('text')}")
    found = {t["name"] for t in data} & WANT
    print("\nFound:", sorted(found))
    print("Missing:", sorted(WANT - found) or "none")


asyncio.run(main())
