"""Reproduce the mobile's POST /job-cards/110/output payload to see the actual error."""
import asyncio
import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()
BASE = os.environ.get("BASE_URL", "http://localhost:8000")


PAYLOAD = {
    "fg_actual_kg": None,
    "fg_actual_units": None,
    "fg_expected_kg": 5000.0,
    "fg_expected_units": 10000,
    "rm_consumed_kg": 2509.6,
    "process_loss_kg": 0.0,
    "byproducts": [
        {"category": "control_sample", "qty_kg": 0.5, "uom": "kg",
         "remarks": "QC Sample"},
    ],
    "balance_materials": [
        # As Android currently builds it (BUG): bal_type="rm", material_name=null
        {"material_id": None, "material_name": None,
         "balance_type": "rm", "qty_kg": 4.0, "remarks": None},
        # Extra giveaway: balance_type="extra_given" (legal) but material_name=null
        {"material_id": None, "material_name": None,
         "balance_type": "extra_given", "qty_kg": 5.1, "remarks": None},
    ],
}


async def main():
    url = f"{BASE}/api/v1/production/job-cards/110/output"
    print("POST", url)
    print("body:", json.dumps(PAYLOAD, indent=2))
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, json=PAYLOAD)
    print("status:", r.status_code)
    print("body:", r.text)

    # Also test V1 endpoint that the Retrofit interface declares
    url_v1 = f"{BASE}/api/v1/production/job-cards/110/record-output"
    print("\nPUT", url_v1)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.put(url_v1, json=PAYLOAD)
    print("status:", r.status_code)
    print("body:", r.text)


asyncio.run(main())
