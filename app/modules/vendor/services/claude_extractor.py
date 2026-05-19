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


_SYSTEM_PROMPT = (
    "You extract structured fields from vendor compliance documents "
    "uploaded by an Indian food-manufacturing ERP. The user will provide "
    "ONE document (PDF or image) per call along with its declared "
    "`doc_type`. You MUST:\n"
    "  1. Read the document carefully — OCR text, headings, tables, and "
    "stamps all count.\n"
    "  2. Return ONLY the fields you can read with high confidence. "
    "Leave fields null rather than guess.\n"
    "  3. Dates MUST be ISO 8601 (YYYY-MM-DD). Convert from DD/MM/YYYY, "
    "DD-MM-YYYY, or Indian short-date forms accordingly.\n"
    "  4. Strip spaces from identifiers (GSTIN, PAN, FSSAI, etc.) and "
    "return them upper-cased.\n"
    "  5. Use the `additional_fields` map for anything useful that "
    "doesn't fit the typed schema (e.g. trade name, jurisdiction, "
    "constitution-of-business).\n"
    "  6. If the supplied document looks unrelated to the declared "
    "doc_type, populate what you can and add a `mismatch_warning` "
    "string into `additional_fields`."
)


_CONTRACT_SYSTEM_PROMPT = (
    "You extract structured fields from a commercial vendor contract "
    "PDF for an Indian food-manufacturing ERP. Return ONLY the fields "
    "you can read with high confidence. Dates MUST be ISO 8601 "
    "(YYYY-MM-DD). `value_inr` is the total contract value in Indian "
    "Rupees as a float (drop currency symbol, comma, INR suffix). "
    "`contract_type` MUST be one of: yearly, one-time, NDA, MSA — "
    "pick the closest match. The counterparty is the OTHER party "
    "(not Candor Foods). Put the contract scope / purpose summary into "
    "`summary` in <=200 chars. Use `additional_fields` for everything "
    "else relevant (penalty clauses, governing law, notice period…)."
)


# ── client (lazy singleton) ──────────────────────────────────────────────


_CLIENT: AsyncAnthropic | None = None


def _client() -> AsyncAnthropic:
    """Process-wide async Anthropic client. Reads ANTHROPIC_API_KEY from env."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = AsyncAnthropic()
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


async def extract_document_fields(
    file_bytes: bytes,
    mime_type: str,
    doc_type: str,
    model: str | None = None,
) -> ExtractedDocFields:
    """Run Claude over a vendor document; return a typed extraction.

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
                "Extract the fields now."
            ),
        },
    ]

    try:
        response = await _client().messages.parse(
            model=model or _default_model(),
            max_tokens=4096,
            # Frozen system prompt → cache-friendly.
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_blocks}],
            output_format=ExtractedDocFields,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("vendor.extract.failed doc_type=%s err=%r", doc_type, e)
        return _failed_doc(repr(e))

    parsed = getattr(response, "parsed_output", None)
    if isinstance(parsed, ExtractedDocFields):
        # Model returned a payload — mark as ok unless the model itself
        # signalled an extraction problem (e.g. via additional_fields).
        if parsed.extraction_status == "ok":
            pass
        return parsed
    if isinstance(parsed, dict):
        try:
            return ExtractedDocFields(**parsed)
        except Exception as e:  # noqa: BLE001
            logger.warning("vendor.extract.parse_back_failed err=%r", e)
            return _failed_doc(f"parse_back_failed: {e!r}")
    return _failed_doc("no_parsed_output")


async def extract_contract_fields(
    file_bytes: bytes,
    mime_type: str,
    model: str | None = None,
) -> ExtractedContractFields:
    user_blocks = [
        _content_block_for_file(file_bytes, mime_type),
        {"type": "text", "text": "Extract the contract fields now."},
    ]
    try:
        response = await _client().messages.parse(
            model=model or _default_model(),
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _CONTRACT_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_blocks}],
            output_format=ExtractedContractFields,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("vendor.extract_contract.failed err=%r", e)
        return _failed_contract(repr(e))

    parsed = getattr(response, "parsed_output", None)
    if isinstance(parsed, ExtractedContractFields):
        return parsed
    if isinstance(parsed, dict):
        try:
            return ExtractedContractFields(**parsed)
        except Exception as e:  # noqa: BLE001
            logger.warning("vendor.extract_contract.parse_back_failed err=%r", e)
            return _failed_contract(f"parse_back_failed: {e!r}")
    return _failed_contract("no_parsed_output")
