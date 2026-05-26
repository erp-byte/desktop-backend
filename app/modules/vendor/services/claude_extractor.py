"""Claude-API-backed structured field extraction for vendor documents.

Uses `claude-sonnet-4-6` with `messages.parse()` for typed structured
extraction. The model is overridable via the `VENDOR_DOC_MODEL` env var.

Prompt-caching note: the system prompt and per-doc-type instructions
are frozen and applied with `cache_control={"type": "ephemeral"}` so the
1.25x write premium amortizes after the second call. Volatile content
(the file bytes) goes AFTER the system block, never inside it.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel

from app.modules.vendor.schemas import ExtractedContractFields, ExtractedDocFields

logger = logging.getLogger(__name__)


_DEFAULT_MODEL_FALLBACK = "claude-sonnet-4-6"


def _default_model() -> str:
    """Late-read VENDOR_DOC_MODEL so test harnesses / hot-reload changes
    are picked up without re-importing the module."""
    return os.environ.get("VENDOR_DOC_MODEL") or _DEFAULT_MODEL_FALLBACK


# ── per-doc-type extraction prompts ──────────────────────────────────────


_DOC_TYPE_INSTRUCTIONS: dict[str, str] = {
    "FSSAI": (
        "This is an Indian FSSAI food safety license. "
        "Extract the 14-digit FSSAI license number into `doc_number`. "
        "Capture issue date in `issued_on`, the validity start in `valid_from`, "
        "expiry in `valid_to`, and put the FSSAI office / regional authority "
        "in `issuing_authority`. Put the FBO / business name in `business_name`."
    ),
    "GST": (
        "This is an Indian GST registration certificate. "
        "Extract the 15-character GSTIN into `doc_number`. "
        "Put the legal name in `business_name`, the principal place of business "
        "in `address`, and the registration date in `valid_from`. "
        "Date of validity (if printed) goes in `valid_to`."
    ),
    "PAN": (
        "This is an Indian PAN card. "
        "Extract the 10-character PAN (5 letters, 4 digits, 1 letter) into "
        "`doc_number`. Put the holder's name in `holder_name`. "
        "PAN cards have no expiry — leave `valid_to` null."
    ),
    "MSME": (
        "This is an Indian MSME registration certificate. "
        "Extract the registration number into `doc_number`. "
        "Put the enterprise name in `business_name`, registration date in "
        "`valid_from`, and the MSME category (Micro / Small / Medium) in "
        "`category`."
    ),
    "UDYAM": (
        "This is an Indian UDYAM registration certificate. "
        "Extract the UDYAM number (format: UDYAM-XX-00-0000000) into "
        "`doc_number`. Put the enterprise name in `business_name`, "
        "registration date in `valid_from`, and MSME category "
        "(Micro / Small / Medium) in `category`."
    ),
    "IEC": (
        "This is an Indian Importer Exporter Code (IEC) certificate. "
        "Extract the 10-digit IEC into `doc_number`. Put the issuing DGFT "
        "office in `issuing_authority` and date of issue in `issued_on`."
    ),
    "BRC": (
        "This is a BRC (British Retail Consortium) global standards "
        "certificate. Extract the certificate number into `doc_number`. "
        "Put issue date in `issued_on`, expiry in `valid_to`, and the "
        "certification body in `issuing_authority`."
    ),
    "EPR": (
        "This is an EPR (Extended Producer Responsibility) registration "
        "certificate. Extract the EPR registration number into `doc_number`. "
        "Issue date in `issued_on`, expiry in `valid_to`."
    ),
    "CIN": (
        "This is a Corporate Identification Number certificate of "
        "incorporation. Extract the 21-character CIN into `doc_number`. "
        "Put company name in `business_name`, registered office in `address`, "
        "and date of incorporation in `valid_from`."
    ),
    "TIN": (
        "This is a Tax Identification Number / VAT registration. "
        "Extract the TIN into `doc_number`. Put the business name in "
        "`business_name` and registered address in `address`."
    ),
    "TAN": (
        "This is a Tax Deduction Account Number certificate. "
        "Extract the 10-character TAN into `doc_number`. Holder name in "
        "`holder_name`."
    ),
    "POLLUTION": (
        "This is a pollution control board consent / EPR-aligned certificate. "
        "Extract the consent number into `doc_number`. Issue date in "
        "`issued_on`, expiry in `valid_to`, issuing pollution control board "
        "in `issuing_authority`."
    ),
    "CONTRACT": (
        "This is a commercial contract document. Treat it as a generic "
        "document — populate `doc_number` from any contract reference "
        "number, `valid_from` from the contract effective date, and "
        "`valid_to` from the contract end date if present."
    ),
    "OTHER": (
        "Treat this as a generic vendor compliance / supporting document. "
        "Populate whatever identifier appears as the primary number into "
        "`doc_number` and any visible dates into the appropriate date "
        "fields. Be conservative — leave fields null when unsure."
    ),
}


# System prompts demand a JSON response shape directly — we no longer use
# `messages.parse()` because Anthropic's grammar compiler chokes on the
# ExtractedDocFields schema (10 nullable strings × an enum produces enough
# state-space combinations that compilation times out server-side). Plain
# `messages.create()` + Pydantic-side validation is more robust and gives
# the same end result.
_SYSTEM_PROMPT = (
    "You extract structured fields from Indian vendor documents — "
    "compliance certificates, invoices, MOAs, registrations, bank "
    "papers, contracts — for a food-manufacturing ERP. The user gives "
    "you ONE document per call with its declared `doc_type`.\n\n"
    "Extract EVERY field you can find. A single document commonly "
    "carries multiple identifiers and unrelated extras — a GST "
    "certificate shows the PAN inside the GSTIN, an invoice carries "
    "GSTIN + PAN + CIN + contact + sometimes bank details, a FSSAI "
    "cert lists 'Kind of Business' which tells us the supplier "
    "category. Pull each one into its named slot.\n\n"
    "Rules:\n"
    "  1. Read carefully — OCR text, headings, tables, stamps, footer "
    "fine-print all count.\n"
    "  2. Use null when the field is not present or not legible. Never "
    "guess.\n"
    "  3. Identifiers (gstin, pan, cin, fssai, iec, udyam, tin, tan, "
    "pollution_epr, brc_other): upper-cased, no spaces, exact format.\n"
    "  4. Dates: ISO 8601 strings (YYYY-MM-DD). Convert from DD/MM/YYYY, "
    "DD-MM-YYYY, '13-Sep-2018', etc.\n"
    "  5. Address: put the full free-text address in `address`; ALSO "
    "split city/state/pin_code into their own fields when the doc "
    "lists them separately.\n"
    "  6. doc_number is the PRIMARY identifier for this doc_type — "
    "set it to the same value you put in the matching typed slot "
    "(gstin for GST cert, pan for PAN card, fssai for FSSAI license, "
    "etc).\n"
    "  7. Supplier metadata: `business_category` is a single human "
    "label like 'Manufacturer', 'Trader', 'Restaurant', 'Service "
    "Provider', 'Distributor' — read it from FSSAI's 'Kind of "
    "Business' / GST's nature-of-business / invoice context. "
    "`supplier_type` is the same idea but more general (Manufacturer "
    "/ Trader / Service Provider / Importer). `nature_of_business` "
    "is a more detailed phrase. `core_business` is a one-line plain-"
    "English description of what they do.\n"
    "  8. supplier_reg_year: year of incorporation / registration. "
    "On a CIN it's the 4 digits at positions 8-11. On registration "
    "certs it's the 'Registered on' year. Use 4-digit year string "
    "(e.g. '2015') or 'YYYY-YY' range if the doc shows fiscal year.\n"
    "  9. Banking fields (bank_name, bank_branch, bank_account_no, "
    "bank_account_name, bank_ifsc, bank_swift, bank_account_type): "
    "ONLY populate these if the document is a cancelled cheque, "
    "passbook page, bank letter, or invoice/letterhead that prints "
    "the supplier's account details. Leave null on PAN / GST / FSSAI "
    "/ MSME etc. Account number must be the full number with no "
    "redaction (`XXXX1234` is not acceptable).\n"
    "  10. Contract fields (contract_type, contract_value, "
    "contract_summary): ONLY populate these when the document is "
    "itself a commercial contract / MSA / NDA / SLA / supply "
    "agreement. `contract_type` is one of: yearly, one-time, NDA, "
    "MSA. `contract_value` is the total INR value as a float (drop "
    "₹ / Rs. / commas). `contract_summary` is <= 200 chars.\n"
    "  11. If the document doesn't match the declared doc_type, "
    "populate what you can and start `additional_notes` with a "
    "one-line warning.\n\n"
    "Return ONLY a JSON object (no prose, no markdown fence) with "
    "these keys (omit or null any you can't read):\n"
    "  doc_number, gstin, pan, cin, fssai, iec, udyam, tin, tan, "
    "pollution_epr, brc_other, issued_on, valid_from, valid_to, "
    "issuing_authority, business_name, holder_name, contact_person, "
    "designation, business_category, supplier_type, nature_of_business, "
    "core_business, supplier_reg_year, address, city, state, pin_code, "
    "mobile, phone, email, website, bank_name, bank_branch, "
    "bank_account_no, bank_account_name, bank_ifsc, bank_swift, "
    "bank_account_type, contract_type, contract_value, "
    "contract_summary, category, additional_notes\n\n"
    "Example response for a GST certificate that also shows the PAN "
    "and lists Kind of Business as 'Manufacturer':\n"
    '{"doc_number": "27AAACI1234A1Z5", "gstin": "27AAACI1234A1Z5", '
    '"pan": "AAACI1234A", "business_name": "Acme Foods Pvt Ltd", '
    '"business_category": "Manufacturer", "supplier_type": "Manufacturer", '
    '"nature_of_business": "Manufacturing of packaged food products", '
    '"address": "12 MG Road, Pune, Maharashtra, 411001", '
    '"city": "Pune", "state": "Maharashtra", "pin_code": "411001", '
    '"valid_from": "2017-07-01", "issuing_authority": "GSTN", '
    '"supplier_reg_year": "2015", "additional_notes": null}'
)


_CONTRACT_SYSTEM_PROMPT = (
    "You extract structured fields from a commercial vendor contract "
    "PDF for an Indian food-manufacturing ERP. Return ONLY the fields "
    "you can read with high confidence. Dates MUST be ISO 8601 strings "
    "(YYYY-MM-DD). `value_inr` is the total contract value in Indian "
    "Rupees as a float (drop currency symbol, comma, INR suffix). "
    "`contract_type` MUST be one of: yearly, one-time, NDA, MSA — "
    "pick the closest match. The counterparty is the OTHER party "
    "(not Candor Foods). Put the contract scope / purpose summary into "
    "`summary` in <=200 chars.\n\n"
    "Return ONLY a JSON object (no prose, no markdown fence) with these "
    "keys (omit a key or set it to null when unknown):\n"
    "  contract_type, signed_date, effective_from, effective_to, "
    "value_inr, counterparty, summary, additional_notes"
)


# ── client (lazy singleton) ──────────────────────────────────────────────


_CLIENT: AsyncAnthropic | None = None


def _resolve_api_key() -> str:
    """Find the Anthropic API key.

    `AsyncAnthropic()` reads `os.environ['ANTHROPIC_API_KEY']` only. But
    pydantic-settings loads `.env` into a `Settings` instance — it does
    NOT export to os.environ. So a key set only in `.env` is invisible to
    the SDK by default. Mirror the same fallback chain that storage.py
    uses for AWS creds: prefer the shell env, then the .env-backed Settings.
    """
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key
    try:
        from app.config import Settings
        return (Settings().ANTHROPIC_API_KEY or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("vendor.extract.settings_load_failed err=%r", e)
        return ""


def _client() -> AsyncAnthropic:
    """Process-wide async Anthropic client."""
    global _CLIENT
    if _CLIENT is None:
        api_key = _resolve_api_key()
        # Pass api_key= explicitly when we have one; otherwise let the SDK
        # raise its own auth error on first call (clearer than ours).
        _CLIENT = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
    return _CLIENT


# ── document content block builder ───────────────────────────────────────


def _content_block_for_file(file_bytes: bytes, mime_type: str) -> dict[str, Any]:
    b64 = base64.standard_b64encode(file_bytes).decode("ascii")
    if mime_type == "application/pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": b64,
            },
        }
    if mime_type in ("image/jpeg", "image/png"):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": b64,
            },
        }
    raise ValueError(f"unsupported mime for extraction: {mime_type}")


# ── public API ───────────────────────────────────────────────────────────


def _failed_doc(err: str) -> ExtractedDocFields:
    return ExtractedDocFields(extraction_status="failed", extraction_error=err)


def _failed_contract(err: str) -> ExtractedContractFields:
    return ExtractedContractFields(extraction_status="failed", extraction_error=err)


import json
import re


# Tolerant JSON extractor — Claude generally returns clean JSON when asked,
# but occasionally wraps it in a ```json fence or prepends a one-line lead.
# We strip the common wrappers + grab the outermost {...} block.
_FENCE_RE   = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
_OBJECT_RE  = re.compile(r"\{[\s\S]*\}")


def _coerce_json_object(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    # Try direct parse first — happy path.
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError):
        pass
    # Fenced block?
    m = _FENCE_RE.search(text)
    if m:
        try:
            v = json.loads(m.group(1))
            return v if isinstance(v, dict) else None
        except (TypeError, ValueError):
            pass
    # First {...} block in the response.
    m = _OBJECT_RE.search(text)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, dict) else None
        except (TypeError, ValueError):
            pass
    return None


def _response_text(response: Any) -> str:
    """Concatenate all text blocks in a Messages API response."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text or "")
    return "".join(parts)


async def extract_document_fields(
    file_bytes: bytes,
    mime_type: str,
    doc_type: str,
    model: str | None = None,
) -> ExtractedDocFields:
    """Run Claude over a vendor document; return a typed extraction.

    Uses `messages.create()` with a JSON-mode prompt (not `messages.parse()`)
    because the structured-output grammar compiler times out on
    ExtractedDocFields. We parse + validate the response client-side with
    Pydantic, which is more permissive (missing keys → defaults, extra
    keys → ignored).

    Never raises — failure modes return an ExtractedDocFields whose
    `extraction_status` is "failed" with `extraction_error` populated.
    The caller can still persist the S3 URL and surface the failure to
    the UI for manual entry.
    """
    instr = _DOC_TYPE_INSTRUCTIONS.get(doc_type, _DOC_TYPE_INSTRUCTIONS["OTHER"])
    user_blocks = [
        _content_block_for_file(file_bytes, mime_type),
        {
            "type": "text",
            "text": (
                f"doc_type: {doc_type}\n\n"
                f"Per-type instructions: {instr}\n\n"
                "Extract the fields now and respond with the JSON object only."
            ),
        },
    ]

    try:
        response = await _client().messages.create(
            model=model or _default_model(),
            max_tokens=2048,
            # Frozen system prompt → cache-friendly per the prompt-caching
            # invariant (any byte change anywhere in the prefix invalidates
            # everything after it).
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_blocks}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("vendor.extract.failed doc_type=%s err=%r", doc_type, e)
        return _failed_doc(repr(e))

    text = _response_text(response)
    data = _coerce_json_object(text)
    if data is None:
        logger.warning(
            "vendor.extract.no_json doc_type=%s text=%r", doc_type, text[:200],
        )
        return _failed_doc(f"no_json_in_response: {text[:200]!r}")

    try:
        return ExtractedDocFields.model_validate(data)
    except Exception as e:  # noqa: BLE001
        logger.warning("vendor.extract.validate_failed doc_type=%s err=%r", doc_type, e)
        return _failed_doc(f"validate_failed: {e!r}")


async def extract_contract_fields(
    file_bytes: bytes,
    mime_type: str,
    model: str | None = None,
) -> ExtractedContractFields:
    user_blocks = [
        _content_block_for_file(file_bytes, mime_type),
        {
            "type": "text",
            "text": "Extract the contract fields now and respond with the JSON object only.",
        },
    ]
    try:
        response = await _client().messages.create(
            model=model or _default_model(),
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": _CONTRACT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_blocks}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("vendor.extract_contract.failed err=%r", e)
        return _failed_contract(repr(e))

    text = _response_text(response)
    data = _coerce_json_object(text)
    if data is None:
        logger.warning("vendor.extract_contract.no_json text=%r", text[:200])
        return _failed_contract(f"no_json_in_response: {text[:200]!r}")

    try:
        return ExtractedContractFields.model_validate(data)
    except Exception as e:  # noqa: BLE001
        logger.warning("vendor.extract_contract.validate_failed err=%r", e)
        return _failed_contract(f"validate_failed: {e!r}")
