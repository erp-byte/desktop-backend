"""Consolidated extractor: walks every PDF in every zip + the 3 combined PDFs
in C:/Users/cando/Downloads/Inventory calc/ and emits ONE CSV with structured
line-items.

Output: d:/Consumption/New/Backend/_all_invoices_extracted.csv
"""
from __future__ import annotations

import csv
import gc
import os
import re
import sys
import time
import traceback
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

ROOT = Path(r"C:\Users\cando\Downloads\Inventory calc")
TMP_BASE = Path(r"C:\Users\cando\AppData\Local\Temp\all_invoices")
OUT_CSV = Path(r"d:\Consumption\New\Backend\_all_invoices_extracted.csv")

DAILY_ZIPS = [
    "120526.zip",
    "130526.zip",
    "140526.zip",
    "150526.zip",
    "160526.zip",
    "180526.zip",
]
APMC_DAILY_ZIPS = [
    "APMC Sales Invoices (12th May).zip",
    "APMC Sales Invoices (13th May).zip",
    "APMC Sales Invoices (14th May).zip",
    "APMC Sales Invoices (15th May).zip",
    "APMC Sales Invoices (16th May).zip",
    "APMC Sales Invoices (18th May).zip",
]
BULK_ZIP = "26-27 Invoice+CN Pdfs.zip"
COMBINED_PDFS = [
    ("CFPL_CN_COMBINED", "CFPL Credit Notes [From 01st April 2026 To 18th May 2026].pdf", "CN", "CFPL"),
    ("CDPL_CN_COMBINED", "CDPL Credit Notes [From 01st April 2026 To18th May 2026].pdf", "CN", "CDPL"),
    ("APMC_INV_COMBINED", "APMC Sales Invoices [From 01st April 2026 To 18th May 2026].pdf", "INV", "CFPL"),
]

# ---------------------------------------------------------------------------
# Regex / helpers (reused / extended from _pdf_extract.py)
# ---------------------------------------------------------------------------

DOC_NO_RE = re.compile(
    r"(?:Credit Note No\.|Invoice No\.\s*:?|Invoice No\.)\s*[:\s]*"
    r"([A-Z0-9][A-Z0-9/\-]{3,}\d)"
)
DOC_NO_FALLBACK_RE = re.compile(
    r"\b(CF/CN/\d+-\d+/\d+|CD/CN/\d+-\d+/\d+|CND?/\d+-\d+/\d+|CF53/\d+/\d+|"
    r"CDPL/\d+-\d+/\d+|SR/CF/\d+-\d+/\d+|CF/\d+-\d+/\d+|CF-SO/\d+-\d+/\d+)\b"
)
DATE_RE = re.compile(r"(\d{1,2}-[A-Z][a-z]{2}-\d{2})")
NUM_RE = r"[\d,]+(?:\.\d+)?"
HSN_LINE_RE = re.compile(
    rf"^\s*(\d+)?\s*(.*?)\s+(\d{{6,8}})\s+(\d+)\s*%\s+"
    rf"(?:(\d+)\s+)?"
    rf"({NUM_RE})\s+(NOS|No|Nos|Kgs|KGS|kgs|PCS|Pic|PIC|GMS|gms)\s+"
    rf"({NUM_RE})\s+(NOS|No|Nos|Kgs|KGS|kgs|PCS|Pic|PIC|GMS|gms)"
    rf"(?:\s+\d+\s*%)?"
    rf"\s+({NUM_RE})\s*$",
    re.IGNORECASE,
)
HSN_SHORT_RE = re.compile(
    rf"^\s*(\d+)?\s*(.*?)\s+(\d{{6,8}})\s+(\d+)\s*%\s+({NUM_RE})\s*$",
    re.IGNORECASE,
)
KGS_LINE_RE = re.compile(r"^\s*\(\s*([\d,]+(?:\.\d+)?)\s*Kgs?\s*\)\s*$", re.IGNORECASE)
PAGE_CONT_RE = re.compile(r"\(Page\s+\d+\)", re.IGNORECASE)
PAGE_CONT2_RE = re.compile(r"^Credit Note\s*\(Page\s*\d+\)", re.IGNORECASE)

# Unit detection — strip spaces in header for fuzzy match. Order matters
# (more specific first).
UNIT_PATTERNS: list[tuple[str, str]] = [
    ("W-202", r"W[-\s]*202|W202"),
    ("A-185", r"A[-\s]*185|A185"),
    ("F-53",  r"F[-\s]*53|F53"),
    ("A-68",  r"A[-\s]*68|A68"),
    ("D-39",  r"D[-\s]*39|D39"),
    ("D-514", r"D[-\s]*514|D514"),
    ("APMC",  r"APMC"),
    ("Pawane", r"PAWANE"),
    ("Cold Storage", r"COLD\s*STORAGE"),
    ("Rishi", r"RISHI"),
    ("Savla", r"SAVLA"),
]


def _num(s: str) -> float:
    if not s:
        return 0.0
    return float(s.replace(",", ""))


def _detect_unit(header_text: str, filename: str = "") -> str:
    """Return canonical unit string. Search PAGE first (header text), then filename."""
    haystack_page = header_text.upper()
    haystack_file = filename.upper()
    # Specific facility codes first (W-202, A-185, F-53, A-68, D-39, D-514)
    for canon, pat in UNIT_PATTERNS[:6]:
        if re.search(pat, haystack_page) or re.search(pat, haystack_file):
            return canon
    # Generic APMC (only if no specific F-53/A-185 etc matched)
    for canon, pat in UNIT_PATTERNS[6:]:
        if re.search(pat, haystack_page) or re.search(pat, haystack_file):
            return canon
    return "OTHER"


def _dedupe_double(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    n = len(s)
    if n >= 6 and n % 2 == 0:
        half = n // 2
        if s[:half].strip() == s[half:].strip():
            return s[:half].strip()
    for sep in ("  ", " "):
        if sep in s:
            parts = s.split(sep)
            mid = len(parts) // 2
            if mid > 0 and parts[:mid] == parts[mid : mid * 2]:
                return sep.join(parts[:mid]).strip()
    return s


def _detect_buyer(lines: list[str]) -> str:
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Buyer (Bill to)") or s.lower().startswith("buyer (bill to)"):
            if i + 1 < len(lines):
                return _dedupe_double(lines[i + 1].strip())
        if s == "Billed To Ship To" or s == "Billed To":
            if i + 1 < len(lines):
                return _dedupe_double(lines[i + 1].strip())
    for i, ln in enumerate(lines):
        if "Consignee (Ship to)" in ln:
            for j in range(i + 1, min(i + 4, len(lines))):
                cand = lines[j].strip()
                if cand and "Dispatch" not in cand:
                    return _dedupe_double(cand)
    return ""


def _detect_doc_no(text: str) -> str:
    m = DOC_NO_RE.search(text)
    if m:
        cand = m.group(1).strip()
        if "/" in cand or "-" in cand:
            return cand
    m2 = DOC_NO_FALLBACK_RE.search(text)
    if m2:
        return m2.group(1)
    return ""


def _detect_date(text: str) -> str:
    head = "\n".join(text.split("\n")[:40])
    m = DATE_RE.search(head)
    if m:
        return m.group(1)
    m2 = DATE_RE.search(text)
    return m2.group(1) if m2 else ""


def _detect_entity(text: str) -> str:
    t = text.upper()
    if "CANDOR DATES PRIVATE LIMITED" in t or "CDPL" in t:
        # If both appear (CN may quote CDPL/...), prefer the company name match
        if "CANDOR DATES PRIVATE LIMITED" in t:
            return "CDPL"
        return "CDPL"
    if "CANDOR FOODS PRIVATE LIMITED" in t:
        return "CFPL"
    return ""


def _detect_doc_type(text: str, filename: str) -> str:
    head_low = "\n".join(text.split("\n")[:5]).lower()
    if "credit note" in head_low:
        return "CN"
    fn_up = Path(filename).name.upper()
    # filename markers: CN65, CND/26-27/811, {CN52} etc
    if re.search(r"\bCN[D]?\b|\{CN\d+\}|/CN/|CND", fn_up):
        return "CN"
    return "INV"


def _is_continuation(text: str) -> bool:
    head = "\n".join(text.split("\n")[:3])
    return bool(PAGE_CONT2_RE.search(head)) or "(Page 2)" in head or "(Page 3)" in head


def _is_eway_bill(text: str) -> bool:
    head = "\n".join(text.split("\n")[:5])
    return head.lstrip().startswith("e-Way Bill") and "Tax Invoice" in head


def _is_cancelled(text: str, filename: str) -> bool:
    fn_up = Path(filename).name.upper()
    if "[CANCELLED]" in fn_up or "CANCELLED" in fn_up:
        return True
    return False


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
    source_zip: str
    source_pdf: str
    page: int


def parse_page(text: str, ctx: dict, page_idx: int, failures: list) -> list[LineItem]:
    if not text.strip():
        return []
    if _is_continuation(text):
        return []
    if _is_eway_bill(text):
        return []

    lines = text.split("\n")
    head_join = "\n".join(lines[:25])

    # Doc-type / entity detection per page (override by ctx fallback)
    doc_type = _detect_doc_type(text, ctx["filename"])
    if ctx.get("doc_type_override"):
        doc_type = ctx["doc_type_override"]

    entity = _detect_entity(head_join) or ctx.get("entity_default", "")
    unit = _detect_unit(head_join, ctx["filename"])

    doc_no = _detect_doc_no(text)
    date = _detect_date(text)
    buyer = _detect_buyer(lines)

    items: list[LineItem] = []
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

            qty_kg = 0.0
            if qty_unit.lower() == "kgs":
                qty_kg = _num(qty_str)
            else:
                # look for (XX.XXXX Kgs)
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
                    if re.match(
                        r"^\s*(Total|CGST|SGST|IGST|Tax|Amount|Bank|Remarks|continued)",
                        nl, re.IGNORECASE,
                    ):
                        break
                    if nl.strip() and not re.match(r"^\s*\d+\s+", nl):
                        desc_tail.append(nl.strip())
                    j += 1
                if desc_tail:
                    description = (description + " " + " ".join(desc_tail)).strip()

            description = re.sub(r"\s+", " ", description).strip()
            items.append(
                LineItem(
                    doc_type=doc_type,
                    doc_no=doc_no,
                    date=date,
                    entity=entity,
                    unit=unit,
                    buyer=buyer,
                    product=description,
                    qty_kg=qty_kg,
                    hsn=hsn,
                    amount=_num(amount_str),
                    source_zip=ctx["source_zip"],
                    source_pdf=ctx["source_pdf"],
                    page=page_idx + 1,
                )
            )
            i += 1
            continue

        ms = HSN_SHORT_RE.match(ln)
        if ms:
            desc_inline = (ms.group(2) or "").strip()
            if not re.search(
                r"^(CGST|SGST|IGST|Total|Tax|Taxable|Less|Rounding|Value|Amount Chargeable)",
                desc_inline, re.IGNORECASE,
            ):
                hsn = ms.group(3)
                amount_str = ms.group(5)
                description = re.sub(r"^\d+\s*", "", desc_inline)
                desc_tail = []
                j = i + 1
                while j < len(lines):
                    nl = lines[j]
                    if HSN_LINE_RE.match(nl) or HSN_SHORT_RE.match(nl):
                        break
                    if KGS_LINE_RE.match(nl):
                        break
                    if re.match(
                        r"^\s*(Total|CGST|SGST|IGST|Tax|Amount Chargeable|Bank|Remarks|continued|Rounding|Less)",
                        nl, re.IGNORECASE,
                    ):
                        break
                    if nl.strip() and not re.match(r"^\s*\d+\s+", nl):
                        desc_tail.append(nl.strip())
                    j += 1
                if desc_tail:
                    description = (description + " " + " ".join(desc_tail)).strip()
                description = re.sub(r"\s+", " ", description).strip()
                items.append(
                    LineItem(
                        doc_type=doc_type,
                        doc_no=doc_no,
                        date=date,
                        entity=entity,
                        unit=unit,
                        buyer=buyer,
                        product=description,
                        qty_kg=0.0,
                        hsn=hsn,
                        amount=_num(amount_str),
                        source_zip=ctx["source_zip"],
                        source_pdf=ctx["source_pdf"],
                        page=page_idx + 1,
                    )
                )
                i += 1
                continue
        i += 1

    if not items and doc_no:
        failures.append(
            (ctx["source_zip"], ctx["source_pdf"], page_idx + 1, doc_no, "no HSN line items parsed")
        )
    return items


def process_pdf(pdf_path: Path, ctx: dict, all_items: list, failures: list) -> tuple[int, int]:
    """Process a single PDF, return (pages, items_added)."""
    n_pages = 0
    n_items = 0
    try:
        with pdfplumber.open(pdf_path) as pdf:
            n_pages = len(pdf.pages)
            for idx, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text() or ""
                except Exception as e:  # noqa: BLE001
                    failures.append(
                        (ctx["source_zip"], ctx["source_pdf"], idx + 1, "", f"extract_text error: {e}")
                    )
                    continue
                try:
                    items = parse_page(text, ctx, idx, failures)
                except Exception as e:  # noqa: BLE001
                    failures.append(
                        (ctx["source_zip"], ctx["source_pdf"], idx + 1, "", f"parse error: {e}")
                    )
                    continue
                for it in items:
                    all_items.append(it)
                    n_items += 1
    except Exception as e:  # noqa: BLE001
        failures.append((ctx["source_zip"], ctx["source_pdf"], 0, "", f"open error: {e}"))
    finally:
        gc.collect()
    return n_pages, n_items


# ---------------------------------------------------------------------------
# Zip / PDF walker
# ---------------------------------------------------------------------------

def extract_zip(zip_path: Path, dest: Path) -> list[Path]:
    """Extract zip into dest dir; return list of .pdf paths inside (recursive)."""
    dest.mkdir(parents=True, exist_ok=True)
    # Skip extraction if already extracted (sanity: check if any .pdf exists)
    existing_pdfs = list(dest.rglob("*.pdf"))
    if existing_pdfs:
        return existing_pdfs
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest)
    return list(dest.rglob("*.pdf"))


def main() -> int:
    t_start = time.time()
    all_items: list[LineItem] = []
    failures: list = []
    seen_doc_no: dict[str, str] = {}  # doc_no -> source_zip (first writer wins)
    per_source_stats: list[dict] = []

    def _add_items(new_items: list[LineItem], source_priority: int):
        """Add items; dedupe by doc_no.
        Priority: lower number = higher priority (kept).
        We track first-seen by (doc_no, source). When a higher-priority source
        appears later, we should replace; otherwise skip.
        Simplest impl: collect everything, dedupe at the end via source order.
        """
        all_items.extend(new_items)

    # --- 1. Daily zips (priority 1 — freshest) ---
    for zname in DAILY_ZIPS:
        zpath = ROOT / zname
        if not zpath.exists():
            print(f"MISSING: {zpath}", file=sys.stderr)
            continue
        dest = TMP_BASE / zpath.stem
        t0 = time.time()
        pdfs = extract_zip(zpath, dest)
        n_pdfs = 0
        n_pages = 0
        n_items = 0
        for pdf in pdfs:
            if _is_cancelled("", str(pdf)):
                continue
            ctx = {
                "source_zip": zname,
                "source_pdf": str(pdf.relative_to(dest)),
                "filename": str(pdf),
                "entity_default": "CFPL",
            }
            p, c = process_pdf(pdf, ctx, all_items, failures)
            n_pages += p
            n_items += c
            n_pdfs += 1
        per_source_stats.append(
            {"src": zname, "pdfs": n_pdfs, "pages": n_pages, "items": n_items, "secs": round(time.time() - t0, 1)}
        )
        print(f"[daily] {zname}: pdfs={n_pdfs} pages={n_pages} items={n_items} ({round(time.time() - t0, 1)}s)")

    # --- 2. APMC daily zips (priority 2) ---
    for zname in APMC_DAILY_ZIPS:
        zpath = ROOT / zname
        if not zpath.exists():
            print(f"MISSING: {zpath}", file=sys.stderr)
            continue
        dest = TMP_BASE / Path(zname).stem
        t0 = time.time()
        pdfs = extract_zip(zpath, dest)
        n_pdfs = 0
        n_pages = 0
        n_items = 0
        for pdf in pdfs:
            ctx = {
                "source_zip": zname,
                "source_pdf": str(pdf.relative_to(dest)),
                "filename": str(pdf),
                "entity_default": "CFPL",
                "doc_type_override": "INV",
            }
            p, c = process_pdf(pdf, ctx, all_items, failures)
            n_pages += p
            n_items += c
            n_pdfs += 1
        per_source_stats.append(
            {"src": zname, "pdfs": n_pdfs, "pages": n_pages, "items": n_items, "secs": round(time.time() - t0, 1)}
        )
        print(f"[apmc-daily] {zname}: pdfs={n_pdfs} pages={n_pages} items={n_items} ({round(time.time() - t0, 1)}s)")

    # --- 3. Bulk zip (priority 3) ---
    zpath = ROOT / BULK_ZIP
    if zpath.exists():
        dest = TMP_BASE / Path(BULK_ZIP).stem
        t0 = time.time()
        pdfs = extract_zip(zpath, dest)
        n_pdfs = 0
        n_pages = 0
        n_items = 0
        for pdf in pdfs:
            if _is_cancelled("", str(pdf)):
                continue
            ctx = {
                "source_zip": BULK_ZIP,
                "source_pdf": str(pdf.relative_to(dest)),
                "filename": str(pdf),
                "entity_default": "CFPL",
            }
            p, c = process_pdf(pdf, ctx, all_items, failures)
            n_pages += p
            n_items += c
            n_pdfs += 1
            if n_pdfs % 100 == 0:
                print(f"  [bulk progress] {n_pdfs}/{len(pdfs)} pdfs, items={n_items}, "
                      f"elapsed={round(time.time() - t0, 1)}s")
        per_source_stats.append(
            {"src": BULK_ZIP, "pdfs": n_pdfs, "pages": n_pages, "items": n_items, "secs": round(time.time() - t0, 1)}
        )
        print(f"[bulk] {BULK_ZIP}: pdfs={n_pdfs} pages={n_pages} items={n_items} ({round(time.time() - t0, 1)}s)")

    # --- 4. Combined PDFs (priority 4) ---
    for label, fname, dtype, entity in COMBINED_PDFS:
        ppath = ROOT / fname
        if not ppath.exists():
            print(f"MISSING: {ppath}", file=sys.stderr)
            continue
        t0 = time.time()
        ctx = {
            "source_zip": "(combined)",
            "source_pdf": fname,
            "filename": str(ppath),
            "entity_default": entity,
            "doc_type_override": dtype,
        }
        p, c = process_pdf(ppath, ctx, all_items, failures)
        per_source_stats.append(
            {"src": fname, "pdfs": 1, "pages": p, "items": c, "secs": round(time.time() - t0, 1)}
        )
        print(f"[combined] {label}: pages={p} items={c} ({round(time.time() - t0, 1)}s)")

    # --- Dedup: prefer earlier source (daily > apmc-daily > bulk > combined) ---
    # Each source appended in priority order; for same doc_no across sources,
    # keep the first occurrence (= highest priority).
    # NOTE: We do NOT dedupe within the same source (a doc may legitimately
    # have multiple line items, each a separate row with same doc_no).
    # The dedup runs at (doc_no, source_zip) level: if doc_no first appeared
    # in priority-N source, drop ALL rows for that doc_no from later sources.
    print("\nDeduplicating across sources...")
    doc_no_first_source: dict[str, str] = {}
    for it in all_items:
        if it.doc_no and it.doc_no not in doc_no_first_source:
            doc_no_first_source[it.doc_no] = it.source_zip

    deduped: list[LineItem] = []
    dropped = 0
    for it in all_items:
        if it.doc_no:
            if doc_no_first_source[it.doc_no] != it.source_zip:
                dropped += 1
                continue
        deduped.append(it)
    print(f"  total rows before dedup: {len(all_items)}")
    print(f"  rows dropped (lower-priority duplicates): {dropped}")
    print(f"  rows kept: {len(deduped)}")

    # --- Write CSV ---
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "doc_type", "doc_no", "date", "entity", "unit", "buyer",
            "product", "qty_kg", "hsn", "amount",
            "source_zip", "source_pdf", "page",
        ])
        for it in deduped:
            w.writerow([
                it.doc_type, it.doc_no, it.date, it.entity, it.unit, it.buyer,
                it.product, f"{it.qty_kg:.4f}", it.hsn, f"{it.amount:.2f}",
                it.source_zip, it.source_pdf, it.page,
            ])

    # --- Reports ---
    print("\n=== Per-source stats ===")
    for s in per_source_stats:
        print(f"  {s['src']}: pdfs={s.get('pdfs', 1)} pages={s['pages']} items={s['items']} ({s['secs']}s)")

    print("\n=== Line-items per doc_type ===")
    by_dt: dict[str, int] = {}
    for it in deduped:
        by_dt[it.doc_type] = by_dt.get(it.doc_type, 0) + 1
    for k, v in sorted(by_dt.items()):
        print(f"  {k}: {v}")

    print("\n=== Total qty_kg per (entity, unit) ===")
    by_eu: dict[tuple[str, str], float] = {}
    for it in deduped:
        key = (it.entity or "(unknown)", it.unit or "OTHER")
        by_eu[key] = by_eu.get(key, 0.0) + it.qty_kg
    for (e, u), q in sorted(by_eu.items()):
        print(f"  {e:10s} {u:14s} {q:>15,.2f}")

    print(f"\nUnique doc_no values: {len(set(it.doc_no for it in deduped if it.doc_no))}")
    print(f"Rows with empty doc_no: {sum(1 for it in deduped if not it.doc_no)}")

    print(f"\nFailures: {len(failures)}")
    for fail in failures[:10]:
        print(" ", fail)

    print(f"\nCSV: {OUT_CSV} ({len(deduped)} rows)")
    print(f"Total elapsed: {round(time.time() - t_start, 1)}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
