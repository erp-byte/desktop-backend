"""Extract structured line-item + unit data from the three combined PDFs.

Outputs: d:/Consumption/New/Backend/_pdf_units_extracted.csv
"""
from __future__ import annotations

import csv
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

ROOT = Path(r"C:\Users\cando\Downloads\Inventory calc")
OUT_CSV = Path(r"d:\Consumption\New\Backend\_pdf_units_extracted.csv")

SOURCES = [
    {
        "label": "CFPL_CN",
        "path": ROOT / "CFPL Credit Notes [From 01st April 2026 To 18th May 2026].pdf",
        "doc_type": "CN",
        "entity_default": "CFPL",
    },
    {
        "label": "CDPL_CN",
        "path": ROOT / "CDPL Credit Notes [From 01st April 2026 To18th May 2026].pdf",
        "doc_type": "CN",
        "entity_default": "CDPL",
    },
    {
        "label": "APMC_INV",
        "path": ROOT / "APMC Sales Invoices [From 01st April 2026 To 18th May 2026].pdf",
        "doc_type": "INV",
        "entity_default": "CFPL",
    },
]

DOC_NO_RE = re.compile(
    r"(?:Credit Note No\.|Invoice No\.\s*:?|Invoice No\.)\s*[:\s]*"
    r"([A-Z0-9][A-Z0-9/\-]{3,}\d)"
)
DOC_NO_FALLBACK_RE = re.compile(r"\b(CF/CN/\d+-\d+/\d+|CD/CN/\d+-\d+/\d+|CND?/\d+-\d+/\d+|CF53/\d+/\d+|CDPL/\d+-\d+/\d+)\b")
DATE_RE = re.compile(r"(\d{1,2}-[A-Z][a-z]{2}-\d{2})")
NUM_RE = r"[\d,]+(?:\.\d+)?"
# Full HSN line with explicit quantity + unit columns
HSN_LINE_RE = re.compile(
    rf"^\s*(\d+)?\s*(.*?)\s+(\d{{6,8}})\s+(\d+)\s*%\s+"
    rf"(?:(\d+)\s+)?"
    rf"({NUM_RE})\s+(NOS|No|Nos|Kgs|KGS|kgs|PCS|Pic|PIC|GMS|gms)\s+"
    rf"({NUM_RE})\s+(NOS|No|Nos|Kgs|KGS|kgs|PCS|Pic|PIC|GMS|gms)"
    rf"(?:\s+\d+\s*%)?"
    rf"\s+({NUM_RE})\s*$",
    re.IGNORECASE,
)
# Short HSN line: e.g. "1Healthy Choice Roasted 20081920 5 % 247.50"
# (rate-difference / value-only adjustments without qty/unit columns)
HSN_SHORT_RE = re.compile(
    rf"^\s*(\d+)?\s*(.*?)\s+(\d{{6,8}})\s+(\d+)\s*%\s+({NUM_RE})\s*$",
    re.IGNORECASE,
)
KGS_LINE_RE = re.compile(r"^\s*\(\s*([\d,]+(?:\.\d+)?)\s*Kgs?\s*\)\s*$", re.IGNORECASE)
PAGE_CONT_RE = re.compile(r"\(Page\s+\d+\)", re.IGNORECASE)
PAGE_CONT2_RE = re.compile(r"^Credit Note\s*\(Page\s*\d+\)", re.IGNORECASE)


def _num(s: str) -> float:
    if not s:
        return 0.0
    return float(s.replace(",", ""))


def _detect_unit(header_text: str) -> str:
    h = header_text.upper().replace(" ", "")
    if "W-202" in h or "W202" in h:
        return "W-202"
    if "A-185" in h or "A185" in h:
        return "A-185"
    if "F-53" in h or "F53" in h or "APMC" in h:
        return "F-53"
    return "OTHER"


def _dedupe_double(s: str) -> str:
    """APMC pages emit "FOO FOO" when the bill-to/ship-to are identical.
    Strip the duplicated tail if the front half equals the back half."""
    s = s.strip()
    if not s:
        return s
    n = len(s)
    if n >= 6 and n % 2 == 0:
        half = n // 2
        if s[:half].strip() == s[half:].strip():
            return s[:half].strip()
    # Handle "FOO FOO" (odd-length, single space separator)
    for sep in ("  ", " "):
        if sep in s:
            parts = s.split(sep)
            mid = len(parts) // 2
            if mid > 0 and parts[:mid] == parts[mid : mid * 2]:
                return sep.join(parts[:mid]).strip()
    return s


def _detect_buyer(lines: list[str]) -> str:
    """Buyer = the line after 'Buyer (Bill to)' or 'Billed To' header."""
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Buyer (Bill to)") or s.lower().startswith("buyer (bill to)"):
            if i + 1 < len(lines):
                return _dedupe_double(lines[i + 1].strip())
        if s == "Billed To Ship To" or s == "Billed To":
            if i + 1 < len(lines):
                return _dedupe_double(lines[i + 1].strip())
    # Fallback: Consignee
    for i, ln in enumerate(lines):
        if "Consignee (Ship to)" in ln:
            # next non-empty line
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j].strip()
                if cand and "Dispatch" not in cand:
                    return _dedupe_double(cand)
    return ""


def _detect_doc_no(text: str) -> str:
    # Try labelled match first
    m = DOC_NO_RE.search(text)
    if m:
        cand = m.group(1).strip()
        # filter junk
        if "/" in cand or "-" in cand:
            return cand
    m2 = DOC_NO_FALLBACK_RE.search(text)
    if m2:
        return m2.group(1)
    return ""


def _detect_date(text: str, doc_no: str) -> str:
    """Pick the first date that appears close to the doc number / header."""
    # Restrict search to first ~40 lines (header region)
    head = "\n".join(text.split("\n")[:40])
    m = DATE_RE.search(head)
    if m:
        return m.group(1)
    m2 = DATE_RE.search(text)
    return m2.group(1) if m2 else ""


def _is_continuation(text: str) -> bool:
    head = "\n".join(text.split("\n")[:3])
    return bool(PAGE_CONT2_RE.search(head)) or "(Page 2)" in head or "(Page 3)" in head


def _is_eway_bill(text: str) -> bool:
    """e-Way Bill duplicate pages in APMC PDF — skip to avoid double-counting."""
    head = "\n".join(text.split("\n")[:5])
    return head.lstrip().startswith("e-Way Bill") and "Tax Invoice" in head


@dataclass
class LineItem:
    doc_type: str
    doc_no: str
    date: str
    entity: str
    unit: str
    buyer: str
    product: str
    qty_kg: float
    hsn: str
    amount: float
    source_pdf: str
    page: int


def parse_page(text: str, src: dict, page_idx: int, failures: list) -> list[LineItem]:
    if not text.strip():
        return []
    if _is_continuation(text):
        # Skip continuation pages so we don't double-count
        return []
    if _is_eway_bill(text):
        # Skip e-Way Bill duplicates (their corresponding tax invoice is elsewhere)
        return []

    lines = text.split("\n")
    head_join = "\n".join(lines[:25])

    # APMC PDFs ship from F-53 (the address line always shows it)
    unit = _detect_unit(head_join)
    if src["label"] == "APMC_INV":
        unit = "F-53"

    doc_no = _detect_doc_no(text)
    date = _detect_date(text, doc_no)
    buyer = _detect_buyer(lines)
    entity = src["entity_default"]

    items: list[LineItem] = []

    # Walk lines looking for HSN lines (per-item).
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = HSN_LINE_RE.match(ln)
        if m:
            desc_inline = (m.group(2) or "").strip()
            hsn = m.group(3)
            qty_str = m.group(6)
            qty_unit = m.group(7)
            amount_str = m.group(10)

            description = re.sub(r"^\d+\s*", "", desc_inline)

            # Compute qty_kg
            qty_kg = 0.0
            if qty_unit.lower() == "kgs":
                qty_kg = _num(qty_str)
            else:
                # Look at next lines for (XX.XXXX Kgs)
                j = i + 1
                desc_tail = []
                while j < len(lines):
                    nl = lines[j]
                    km = KGS_LINE_RE.match(nl)
                    if km:
                        qty_kg = _num(km.group(1))
                        break
                    if HSN_LINE_RE.match(nl) or HSN_SHORT_RE.match(nl):
                        break
                    if re.match(r"^\s*(Total|CGST|SGST|IGST|Tax|Amount|Bank|Remarks|continued)", nl, re.IGNORECASE):
                        break
                    if nl.strip() and not re.match(r"^\s*\d+\s+", nl):
                        desc_tail.append(nl.strip())
                    j += 1
                if desc_tail:
                    description = (description + " " + " ".join(desc_tail)).strip()

            description = re.sub(r"\s+", " ", description).strip()

            items.append(
                LineItem(
                    doc_type=src["doc_type"],
                    doc_no=doc_no,
                    date=date,
                    entity=entity,
                    unit=unit,
                    buyer=buyer,
                    product=description,
                    qty_kg=qty_kg,
                    hsn=hsn,
                    amount=_num(amount_str),
                    source_pdf=src["label"],
                    page=page_idx + 1,
                )
            )
            i += 1
            continue

        # Try short pattern (rate-difference / value-only adjustment)
        ms = HSN_SHORT_RE.match(ln)
        if ms:
            desc_inline = (ms.group(2) or "").strip()
            # Reject obvious false positives: tax/total/header rows
            if not re.search(
                r"^(CGST|SGST|IGST|Total|Tax|Taxable|Less|Rounding|Value|Amount Chargeable)",
                desc_inline,
                re.IGNORECASE,
            ):
                hsn = ms.group(3)
                amount_str = ms.group(5)
                description = re.sub(r"^\d+\s*", "", desc_inline)
                # Collect description continuation lines until next item / total / qualifier
                desc_tail = []
                j = i + 1
                while j < len(lines):
                    nl = lines[j]
                    if HSN_LINE_RE.match(nl) or HSN_SHORT_RE.match(nl):
                        break
                    if KGS_LINE_RE.match(nl):
                        break
                    if re.match(r"^\s*(Total|CGST|SGST|IGST|Tax|Amount Chargeable|Bank|Remarks|continued|Rounding|Less)", nl, re.IGNORECASE):
                        break
                    if nl.strip() and not re.match(r"^\s*\d+\s+", nl):
                        desc_tail.append(nl.strip())
                    j += 1
                if desc_tail:
                    description = (description + " " + " ".join(desc_tail)).strip()
                description = re.sub(r"\s+", " ", description).strip()
                items.append(
                    LineItem(
                        doc_type=src["doc_type"],
                        doc_no=doc_no,
                        date=date,
                        entity=entity,
                        unit=unit,
                        buyer=buyer,
                        product=description,
                        qty_kg=0.0,
                        hsn=hsn,
                        amount=_num(amount_str),
                        source_pdf=src["label"],
                        page=page_idx + 1,
                    )
                )
                i += 1
                continue
        i += 1

    if not items and doc_no:
        # No items parsed but it looked like a real invoice page (had a doc no
        # and was not flagged as continuation).
        failures.append((src["label"], page_idx + 1, doc_no, "no HSN line items parsed"))

    return items


def main() -> int:
    all_items: list[LineItem] = []
    failures: list = []
    per_pdf_stats = []

    for src in SOURCES:
        path = src["path"]
        if not path.exists():
            print(f"MISSING: {path}", file=sys.stderr)
            continue
        t0 = time.time()
        with pdfplumber.open(path) as pdf:
            n_pages = len(pdf.pages)
            n_items = 0
            total_qty = 0.0
            for idx, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text() or ""
                except Exception as e:  # noqa: BLE001
                    failures.append((src["label"], idx + 1, "", f"extract_text error: {e}"))
                    continue
                try:
                    items = parse_page(text, src, idx, failures)
                except Exception as e:  # noqa: BLE001
                    failures.append((src["label"], idx + 1, "", f"parse error: {e}"))
                    continue
                for it in items:
                    all_items.append(it)
                    n_items += 1
                    total_qty += it.qty_kg
        per_pdf_stats.append(
            {
                "label": src["label"],
                "pages": n_pages,
                "items": n_items,
                "qty_kg": total_qty,
                "secs": round(time.time() - t0, 1),
            }
        )
        print(
            f"[{src['label']}] pages={n_pages} items={n_items} qty_kg={total_qty:,.2f} "
            f"in {round(time.time() - t0, 1)}s"
        )

    # Write CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "doc_type",
                "doc_no",
                "date",
                "entity",
                "unit",
                "buyer",
                "product",
                "qty_kg",
                "hsn",
                "amount",
                "source_pdf",
                "page",
            ]
        )
        for it in all_items:
            w.writerow(
                [
                    it.doc_type,
                    it.doc_no,
                    it.date,
                    it.entity,
                    it.unit,
                    it.buyer,
                    it.product,
                    f"{it.qty_kg:.4f}",
                    it.hsn,
                    f"{it.amount:.2f}",
                    it.source_pdf,
                    it.page,
                ]
            )

    # Per-unit breakdown
    print("\n=== Per-unit qty_kg breakdown ===")
    breakdown: dict[tuple[str, str], float] = {}
    for it in all_items:
        key = (it.doc_type, it.unit)
        breakdown[key] = breakdown.get(key, 0.0) + it.qty_kg
    units = ["W-202", "A-185", "F-53", "OTHER"]
    for dt in ("CN", "INV"):
        print(f"{dt}: ", end="")
        bits = []
        for u in units:
            bits.append(f"{u}={breakdown.get((dt, u), 0.0):,.2f}")
        print(" ".join(bits))

    print(f"\nCSV written: {OUT_CSV} ({len(all_items)} rows)")
    print(f"Failures: {len(failures)}")
    print("Top failures:")
    for f in failures[:5]:
        print(" ", f)

    return 0


if __name__ == "__main__":
    sys.exit(main())
