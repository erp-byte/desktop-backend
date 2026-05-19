"""POST /api/v1/po/preview implementation.

Pipeline:
    1. Parse Excel via existing `parser.parse_po_book`.
    2. For each PO:
         a. Match every line against `master_items` via `match_sku`.
         b. Look up existing po_header (entity, po_number) — duplicate detection.
         c. If duplicate: build diff (header) + collect warnings (rate variance).
    3. Compute summary counters.
    4. Return PreviewResponse (no DB writes).

The preview NEVER touches po_header / po_line / po_box. Reads only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.middleware.request_context import AuthError
from modules.purchase.services import po_diff
from modules.purchase.services.parser import parse_po_book
from modules.so.services.item_matcher import MasterItem, match_sku

logger = logging.getLogger(__name__)

_FUZZY_THRESHOLD = 0.70
_EXACT_THRESHOLD = 0.99


# ── helpers ──────────────────────────────────────────────────────────────


def _validate_file(filename: str | None, size: int) -> None:
    if not filename:
        raise AuthError("invalid_file", "No file attached.", 400)
    name = filename.lower()
    if name.endswith(".xls"):
        raise AuthError(
            "invalid_file_type",
            "Legacy .xls is not supported. Save as .xlsx and re-upload.",
            400,
        )
    if not name.endswith(".xlsx"):
        raise AuthError("invalid_file_type", "Only .xlsx files are accepted.", 400)
    if size > 50 * 1024 * 1024:
        raise AuthError("file_too_large", "File too large. Maximum 50 MB.", 400)
    if size == 0:
        raise AuthError("invalid_file", "Uploaded file is empty.", 400)


def _txn_no_for_preview(po_idx: int, base: datetime) -> str:
    """Stable transaction_no for the preview round-trip. The commit step may
    overwrite it; we just need each PO in the preview to be addressable."""
    return f"TXN-PREVIEW-{base.strftime('%Y%m%d%H%M%S')}-{po_idx + 1:03d}"


def _classify_match_source(score: float) -> str:
    if score >= _EXACT_THRESHOLD:
        return "exact"
    if score >= _FUZZY_THRESHOLD:
        return "fuzzy"
    return "none"


def _matched_item_payload(item: MasterItem | None) -> dict | None:
    if item is None:
        return None
    # MasterItem is a dataclass — be tolerant of field name variations
    out = {}
    for attr in ("sku_id", "sku_name", "item_category", "sub_category",
                 "item_type", "sales_group", "gst_rate", "uom"):
        if hasattr(item, attr):
            v = getattr(item, attr)
            if v is not None:
                out[attr] = v
    return out


def _enrich_line_from_master(line: dict, item: MasterItem | None, score: float) -> dict:
    """Return a NEW line dict with master-item fields filled in alongside
    parsed values. Parser-provided values win; master fills gaps."""
    out = dict(line)
    out["match_score"] = round(score, 4) if score else 0.0
    out["match_source"] = _classify_match_source(score)
    out["matched_item"] = _matched_item_payload(item)
    if item is not None:
        for attr, key in (
            ("item_category", "item_category"),
            ("sub_category", "sub_category"),
            ("item_type", "item_type"),
            ("sales_group", "sales_group"),
            ("gst_rate", "gst_rate"),
            ("uom", "uom"),
            ("sku_name", "sku_name"),  # canonical SKU name
        ):
            if hasattr(item, attr) and out.get(key) in (None, ""):
                out[key] = getattr(item, attr)
    return out


# ── main entrypoint ──────────────────────────────────────────────────────


async def preview(
    pool,
    file_bytes: bytes,
    filename: str | None,
    master_items: list[MasterItem],
    entity: str,
) -> dict:
    """Build the PreviewResponse payload. No DB writes.

    Reads:
        • po_header (existence + diff)
        • po_line (rate history for warnings)
    """
    _validate_file(filename, len(file_bytes))

    try:
        parsed = parse_po_book(file_bytes)
    except Exception as e:
        # WR-07: don't echo parser internals (paths, library type names, etc.)
        # to the client — keep the detail in the server log for ops only.
        logger.warning("po.preview.parse_failed file=%s err=%r", filename, e)
        raise AuthError(
            "invalid_file",
            "Could not read Excel file. Verify it's a valid .xlsx workbook.",
            400,
        )

    if not parsed:
        return {
            "summary": {
                "total_pos": 0, "new": 0, "duplicates": 0,
                "matched_lines": 0, "unmatched_lines": 0,
            },
            "pos": [],
        }

    base_time = datetime.now(timezone.utc)
    out_pos: list[dict] = []
    matched_lines = 0
    unmatched_lines = 0
    duplicates = 0

    # Pre-fetch existing headers for the (entity, po_number) tuples we saw,
    # plus rate history per sku — single trip each instead of N round-trips.
    po_numbers = [p.get("po_number") for p in parsed if p.get("po_number")]
    sku_names = list({
        line.get("sku_name") for p in parsed for line in p.get("lines", [])
        if line.get("sku_name")
    })

    existing_by_pono: dict[str, dict] = {}
    if po_numbers:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM po_header
                 WHERE entity = $1 AND po_number = ANY($2::text[])
                   AND deleted_at IS NULL
                """,
                entity, po_numbers,
            )
            for r in rows:
                existing_by_pono[r["po_number"]] = dict(r)

    last_rates: dict[str, float] = {}
    if sku_names:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (l.sku_name) l.sku_name, l.rate
                  FROM po_line l
                  JOIN po_header h ON h.transaction_no = l.transaction_no
                 WHERE h.entity = $1
                   AND l.sku_name = ANY($2::text[])
                   AND h.deleted_at IS NULL
                 ORDER BY l.sku_name, h.po_date DESC NULLS LAST, h.created_at DESC
                """,
                entity, sku_names,
            )
            for r in rows:
                if r["rate"] is not None:
                    last_rates[r["sku_name"]] = float(r["rate"])

    for idx, po in enumerate(parsed):
        po_number = po.get("po_number")
        existing = existing_by_pono.get(po_number) if po_number else None
        is_dup = existing is not None
        if is_dup:
            duplicates += 1

        duplicate_key = f"{entity}|{po_number}" if po_number else f"{entity}|<no-po-number>-{idx+1}"
        txn_no = existing["transaction_no"] if is_dup else _txn_no_for_preview(idx, base_time)

        # Match every line
        enriched_lines: list[dict] = []
        for line in po.get("lines", []):
            sku_name = line.get("sku_name") or ""
            item, score = match_sku(sku_name, master_items, _FUZZY_THRESHOLD) \
                if sku_name else (None, 0.0)
            enriched = _enrich_line_from_master(line, item, score)
            if enriched.get("match_source") in ("exact", "fuzzy"):
                matched_lines += 1
            else:
                unmatched_lines += 1
            enriched_lines.append(enriched)

        # Header dict: parser output minus 'lines' key
        header = {k: v for k, v in po.items() if k != "lines"}
        header["supplier_id"] = header.get("supplier_id")  # placeholder; parser may not set

        # If this is a duplicate, project existing into the same shape for diff
        existing_proj: dict | None = None
        diff: dict | None = None
        if existing:
            existing_proj = {
                k: existing.get(k) for k in (
                    "po_number", "po_date", "voucher_type", "order_reference_no",
                    "narration", "vendor_supplier_name", "supplier_id",
                    "gross_total", "total_amount", "sgst_amount", "cgst_amount",
                    "igst_amount", "round_off", "freight_transport_local",
                    "apmc_tax", "packing_charges", "freight_transport_charges",
                    "loading_unloading_charges", "other_charges_non_gst",
                )
            }
            # Coerce DB Decimal/date types to JSON-friendly forms.
            # WR-04: defensive — float() may not work on every non-primitive
            # type (memoryview, bytes, future schema types). Fall back to
            # str() so a single odd column can't 500 the whole preview.
            for k, v in list(existing_proj.items()):
                if v is None:
                    continue
                if hasattr(v, "isoformat"):
                    existing_proj[k] = v.isoformat() if hasattr(v, "year") else str(v)
                elif not isinstance(v, (str, int, float, bool)):
                    try:
                        existing_proj[k] = float(v)
                    except (TypeError, ValueError):
                        existing_proj[k] = str(v)
            diff = po_diff.header_diff(existing_proj, header)

        warnings = po_diff.line_warnings(enriched_lines, last_rates)

        out_pos.append({
            "is_duplicate": is_dup,
            "duplicate_key": duplicate_key,
            "transaction_no": txn_no,
            "header": header,
            "incoming": header,                 # raw header round-trip
            "existing": existing_proj,
            "diff": diff,
            "lines": enriched_lines,
            "warnings": warnings,
        })

    return {
        "summary": {
            "total_pos": len(out_pos),
            "new": len(out_pos) - duplicates,
            "duplicates": duplicates,
            "matched_lines": matched_lines,
            "unmatched_lines": unmatched_lines,
        },
        "pos": out_pos,
    }
