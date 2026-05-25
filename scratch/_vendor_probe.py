"""End-to-end smoke probe for the vendor management module.

Usage:
    python _vendor_probe.py                    # full run (skip upload if no S3)
    python _vendor_probe.py --skip-upload      # skip upload + Claude steps
    python _vendor_probe.py --base http://localhost:8000

The probe assumes the backend is already running. It logs in as an admin
user (phone + password from env) so the require_permission() checks
pass, runs through every CRUD path, and prints a summary.

Per project convention this is a script, not a pytest test — run it
manually and read the output.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import httpx


DEFAULT_BASE = "http://localhost:8000"


# ── auth ─────────────────────────────────────────────────────────────────


async def _login(client: httpx.AsyncClient, base: str) -> str:
    phone = os.environ.get("PROBE_PHONE", "")
    password = os.environ.get("PROBE_PASSWORD", "")
    if not phone or not password:
        raise SystemExit(
            "Set PROBE_PHONE and PROBE_PASSWORD env vars (admin user) to run the probe."
        )
    r = await client.post(
        f"{base}/api/v1/auth/login",
        json={"phone": phone, "password": password},
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ── per-section probes ──────────────────────────────────────────────────


async def probe_vendor_crud(client: httpx.AsyncClient, base: str, headers: dict) -> str:
    print("\n=== vendor_master CRUD ===")
    create_body = {
        "name": "Probe Vendor Pvt Ltd",
        "status": "active",
        "contact_person": "QA Bot",
        "mobile": "9999999999",
        "email": "qa@example.com",
        "gstn": "27AAAPL1234C1Z5",
        "pan_no": "AAAPL1234C",
        "is_msme": True,
    }
    r = await client.post(f"{base}/api/v1/vendors", headers=headers, json=create_body)
    r.raise_for_status()
    vendor = r.json()
    vendor_id = vendor["vendor_id"]
    print(f"  created vendor_id={vendor_id} supplier_code={vendor['supplier_code']}")
    assert vendor["supplier_code"].startswith("SC"), "auto-generated supplier_code missing"

    r = await client.get(
        f"{base}/api/v1/vendors",
        headers=headers,
        params={"status": "active", "page": 1, "page_size": 5},
    )
    r.raise_for_status()
    body = r.json()
    print(f"  list returned {body['total']} active vendors")

    r = await client.get(f"{base}/api/v1/vendors/{vendor_id}", headers=headers)
    r.raise_for_status()
    detail = r.json()
    assert detail["vendor"]["vendor_id"] == vendor_id
    print(f"  nested GET returned vendor + "
          f"{len(detail['banking'])} banking / {len(detail['documents'])} docs / "
          f"{len(detail['contracts'])} contracts")

    r = await client.patch(
        f"{base}/api/v1/vendors/{vendor_id}",
        headers=headers,
        json={"contact_person": "QA Bot v2"},
    )
    r.raise_for_status()
    print(f"  patched contact_person -> {r.json().get('contact_person')}")
    return vendor_id


async def probe_banking(client: httpx.AsyncClient, base: str, headers: dict, vendor_id: str) -> None:
    print("\n=== vendor_banking ===")
    r = await client.post(
        f"{base}/api/v1/vendors/{vendor_id}/banking",
        headers=headers,
        json={
            "bank_name": "HDFC Bank",
            "account_no": "12345678901234",
            "account_name": "Probe Vendor Pvt Ltd",
            "ifsc": "HDFC0001234",
            "is_primary": True,
            "is_active": True,
        },
    )
    r.raise_for_status()
    bank1 = r.json()
    print(f"  bank1 bank_id={bank1['bank_id']} is_primary={bank1['is_primary']}")

    r = await client.post(
        f"{base}/api/v1/vendors/{vendor_id}/banking",
        headers=headers,
        json={
            "bank_name": "ICICI Bank",
            "account_no": "98765432109876",
            "account_name": "Probe Vendor Pvt Ltd",
            "ifsc": "ICIC0005678",
            "is_primary": True,
            "is_active": True,
        },
    )
    r.raise_for_status()
    bank2 = r.json()
    print(f"  bank2 bank_id={bank2['bank_id']} is_primary={bank2['is_primary']}")

    r = await client.get(f"{base}/api/v1/vendors/{vendor_id}/banking", headers=headers)
    r.raise_for_status()
    banks = r.json()
    primaries = [b for b in banks if b["is_primary"]]
    assert len(primaries) == 1, f"expected exactly 1 primary, got {len(primaries)}"
    print(f"  ✓ exactly one is_primary=true ({primaries[0]['bank_id']})")


async def probe_document_manual(
    client: httpx.AsyncClient, base: str, headers: dict, vendor_id: str,
) -> None:
    print("\n=== vendor_document (manual) ===")
    r = await client.post(
        f"{base}/api/v1/vendors/{vendor_id}/documents",
        headers=headers,
        json={
            "doc_type": "PAN",
            "doc_number": "AAAPL1234C",
            "s3_urls": "https://example.com/pan.pdf",
        },
    )
    r.raise_for_status()
    doc = r.json()
    print(f"  doc_id={doc['doc_id']} s3_urls={doc['s3_urls']!r}")

    r = await client.patch(
        f"{base}/api/v1/vendors/{vendor_id}/documents/{doc['doc_id']}",
        headers=headers,
        json={"s3_urls": "https://example.com/pan.pdf, https://example.com/pan2.pdf"},
    )
    r.raise_for_status()
    upd = r.json()
    assert "," in upd["s3_urls"], "s3_urls should be comma-separated"
    print(f"  ✓ CSV preserved: {upd['s3_urls']!r}")


async def probe_document_upload(
    client: httpx.AsyncClient, base: str, headers: dict, vendor_id: str, sample_pdf: Path | None,
) -> None:
    if sample_pdf is None or not sample_pdf.is_file():
        print("\n=== vendor_document upload (skipped — no sample PDF) ===")
        return
    print(f"\n=== vendor_document upload + Claude extract ({sample_pdf.name}) ===")
    with sample_pdf.open("rb") as f:
        files = {"file": (sample_pdf.name, f.read(), "application/pdf")}
    data = {"doc_type": "FSSAI"}
    r = await client.post(
        f"{base}/api/v1/vendors/{vendor_id}/documents/upload-and-save",
        headers=headers,
        files=files,
        data=data,
    )
    if r.status_code == 503:
        print(f"  storage unavailable: {r.json()}")
        return
    r.raise_for_status()
    payload = r.json()
    print(f"  document doc_id={payload['document']['doc_id']}")
    print(f"  s3_urls={payload['document']['s3_urls']!r}")
    print(f"  extracted={payload['extracted']}")


async def probe_soft_delete(
    client: httpx.AsyncClient, base: str, headers: dict, vendor_id: str,
) -> None:
    print("\n=== soft delete ===")
    r = await client.delete(f"{base}/api/v1/vendors/{vendor_id}", headers=headers)
    assert r.status_code == 204
    # list should not include it
    r = await client.get(f"{base}/api/v1/vendors/{vendor_id}", headers=headers)
    assert r.status_code == 404, f"expected 404 after soft delete, got {r.status_code}"
    print(f"  ✓ vendor {vendor_id} is now soft-deleted")


# ── DB sanity (audit_log row count delta) ───────────────────────────────


async def audit_count(db_url: str, vendor_id: str) -> int:
    conn = await asyncpg.connect(db_url)
    try:
        # audit_log uses (entity_table, entity_pk) — not `record_id`.
        row = await conn.fetchrow(
            """
            SELECT count(*) AS c
              FROM audit_log
             WHERE entity_table = 'vendor_master'
               AND entity_pk = $1
            """,
            vendor_id,
        )
        return int(row["c"]) if row else 0
    finally:
        await conn.close()


# ── entry point ─────────────────────────────────────────────────────────


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.environ.get("PROBE_BASE", DEFAULT_BASE))
    parser.add_argument(
        "--sample-pdf",
        default=os.environ.get("PROBE_SAMPLE_PDF", ""),
        help="path to a sample FSSAI PDF for the upload probe",
    )
    parser.add_argument("--skip-upload", action="store_true",
                        help="skip the upload-and-save step (no S3 / no Claude call)")
    args = parser.parse_args()

    sample_pdf = Path(args.sample_pdf) if args.sample_pdf else None

    async with httpx.AsyncClient(timeout=60.0) as client:
        token = await _login(client, args.base)
        headers = {"Authorization": f"Bearer {token}"}

        vendor_id = await probe_vendor_crud(client, args.base, headers)
        await probe_banking(client, args.base, headers, vendor_id)
        await probe_document_manual(client, args.base, headers, vendor_id)
        if not args.skip_upload:
            await probe_document_upload(client, args.base, headers, vendor_id, sample_pdf)

        db_url = os.environ.get("DATABASE_URL", "")
        if db_url:
            print(f"\n=== audit_log rows for vendor {vendor_id} ===")
            print(f"  {await audit_count(db_url, vendor_id)} captured")

        await probe_soft_delete(client, args.base, headers, vendor_id)

    print("\n✓ probe passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
