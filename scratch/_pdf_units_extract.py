"""Extract unit/factory allocation per line item from inventory PDFs.

Scans the FULL page text (not just the header) and emits
`_pdf_units_extracted.csv` with one row per (invoice/CN, line item) plus the
detected unit and which detection method won.

Priority order (first hit wins):
  1. DISPATCH:    "Dispatched from" / "Dispatch From" / "Ship From" /
                  "From Location" / "Origin" / "Source" / "Dispatch Doc No."
                  region (text within ~4 lines after the label, UNTIL the next
                  labelled section starts).
  2. NARRATION:   "Narration" / "Remarks" / "Other References" / "Notes"
                  region (same scoping rule).
  3. HEADER:      The issuing company's address block — defined as the first
                  ~12 lines of the page, up to the first occurrence of a
                  "Buyer" / "Bill to" / "Consignee" / "Ship to" anchor.
  4. ADDRESS_FB:  Anywhere else in the page (last-resort fallback).

Unit aliases:
  W-?202        -> W-202
  A-?185        -> A-185
  F-?53 | APMC  -> F-53
  A-?68         -> A-68
  cold storage / supreme cold -> COLD STORAGE
  rishi         -> RISHI
  savla / D-?39 / D-?514 -> SAVLA
  A-?101        -> A-101
  pawane        -> PAWANE   (note: also matches buyer addresses; ranked low)

If multiple units appear in the same scope, the one nearest the priority label
wins. If still ambiguous, the most-frequent unit on the page wins and a
warning is logged.

Output CSV columns:
  cn_no, date, buyer, entity, product, qty_kg, hsn, amount,
  unit, unit_source, unit_warning, source_zip, source_pdf

Sources accepted:
  - the three big aggregate PDFs in Downloads/Inventory calc/
  - CN PDFs extracted from daily zips (reuses _cn_extract.is_cn_filename)
"""
from __future__ import annotations

import csv
import re
import sys
import traceback
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

# Re-use header/item parsing from the existing CN extractor so this script
# stays consistent with the rows users already trust.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cn_extract import (  # noqa: E402
    DATE_RE,
    ROW_DATA_RE,
    extract_buyer,
    extract_date,
    infer_entity,
    is_cn_filename,
    parse_line_items,
)

# Override _cn_extract.CN_NO_RE so we also catch CDPL ("CD/CN/...") documents
# in addition to CFPL ("CF/CN/...") and the older CND/...
CN_NO_RE = re.compile(
    r"(CF/CN/\d{2}-\d{2}/\d+|CD/CN/\d{2}-\d{2}/\d+|CND/\d{2}-\d{2}/\d+)",
    re.IGNORECASE,
)


def extract_cn_no(text: str) -> str | None:
    m = CN_NO_RE.search(text)
    return m.group(1) if m else None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DOWNLOADS = Path(r"C:\Users\cando\Downloads")
INVENTORY = DOWNLOADS / "Inventory calc"
TMP = Path(r"C:\Users\cando\AppData\Local\Temp\pdf_units_extract")
OUT_CSV = Path(r"d:\Consumption\New\Backend\_pdf_units_extracted.csv")

TMP.mkdir(parents=True, exist_ok=True)

AGGREGATE_PDFS = [
    INVENTORY / "CFPL Credit Notes [From 01st April 2026 To 18th May 2026].pdf",
    INVENTORY / "CDPL Credit Notes [From 01st April 2026 To18th May 2026].pdf",
    INVENTORY / "APMC Sales Invoices [From 01st April 2026 To 18th May 2026].pdf",
]

ZIP_PATTERNS = [
    re.compile(r"^\d{6}\.zip$", re.IGNORECASE),
    re.compile(r"^APMC Sales Invoices.*\.zip$", re.IGNORECASE),
]

# ---------------------------------------------------------------------------
# Unit alias table
# ---------------------------------------------------------------------------

# Each entry: (compiled-regex, canonical-unit-label, low-confidence?)
# low-confidence patterns are skipped when looking at buyer/consignee
# addresses (because e.g. "Pawane" appears as a Mumbai locality in many
# buyer addresses but does not necessarily indicate the dispatching unit).
UNIT_PATTERNS: list[tuple[re.Pattern[str], str, bool]] = [
    (re.compile(r"W[- ]?202", re.I), "W-202", False),
    (re.compile(r"A[- ]?185\b", re.I), "A-185", False),
    (re.compile(r"F[- ]?53\b", re.I), "F-53", False),
    (re.compile(r"\bAPMC\b", re.I), "F-53", True),  # APMC -> F-53 (low conf)
    (re.compile(r"A[- ]?68\b", re.I), "A-68", False),
    (re.compile(r"supreme\s*cold", re.I), "COLD STORAGE", False),
    (re.compile(r"cold\s*storage", re.I), "COLD STORAGE", False),
    (re.compile(r"\brishi\b", re.I), "RISHI", False),
    (re.compile(r"\bsavla\b", re.I), "SAVLA", False),
    (re.compile(r"\bD[- ]?39\b", re.I), "SAVLA", True),
    (re.compile(r"\bD[- ]?514\b", re.I), "SAVLA", True),
    (re.compile(r"A[- ]?101\b", re.I), "A-101", False),
    (re.compile(r"\bpawane\b", re.I), "PAWANE", True),
]


def find_units_in(span: str, *, allow_low_conf: bool = True) -> list[tuple[str, int]]:
    """Return [(unit, position-in-span)] for every pattern that fires."""
    out: list[tuple[str, int]] = []
    for pat, unit, low in UNIT_PATTERNS:
        if low and not allow_low_conf:
            continue
        for m in pat.finditer(span):
            out.append((unit, m.start()))
    out.sort(key=lambda t: t[1])
    return out


# ---------------------------------------------------------------------------
# Region detection on a page
# ---------------------------------------------------------------------------

DISPATCH_LABELS = (
    "dispatched from",
    "dispatch from",
    "ship from",
    "shipped from",
    "consignor",
    "from location",
    "origin",
    "source",
    "dispatch doc no",
)
NARRATION_LABELS = ("narration", "remarks", "other references", "notes")
BUYER_ANCHORS = ("buyer", "bill to", "consignee", "ship to", "billed to")


def _scope_window(lines: list[str], start: int, max_lines: int = 6) -> str:
    """Return the joined text of the next ~N lines, stopping at the next
    labelled section header."""
    out = []
    for j in range(start, min(start + max_lines, len(lines))):
        ln = lines[j]
        low = ln.lower()
        # next labelled section terminates the scope
        if j > start and any(lab in low for lab in DISPATCH_LABELS + NARRATION_LABELS):
            break
        if j > start and any(a in low for a in BUYER_ANCHORS):
            break
        out.append(ln)
    return "\n".join(out)


def _header_window(text: str) -> str:
    """Return the issuing-company address block: first ~12 lines until the
    first buyer/consignee anchor."""
    lines = text.splitlines()
    end = len(lines)
    for i, ln in enumerate(lines):
        if i > 0 and any(a in ln.lower() for a in BUYER_ANCHORS):
            end = i
            break
        if i >= 12:
            end = 12
            break
    return "\n".join(lines[:end])


@dataclass
class UnitDecision:
    unit: str | None
    source: str  # 'dispatch' | 'narration' | 'header' | 'address-fallback' | 'none'
    warning: str = ""


def detect_unit(page_text: str) -> UnitDecision:
    """Apply the prioritised search to a single page's text."""
    lines = page_text.splitlines()

    # ---- 1) DISPATCH scope ----
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(lab in low for lab in DISPATCH_LABELS):
            scope = _scope_window(lines, i, max_lines=4)
            hits = find_units_in(scope, allow_low_conf=False)
            if hits:
                return UnitDecision(hits[0][0], "dispatch")

    # ---- 2) NARRATION scope ----
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(lab in low for lab in NARRATION_LABELS):
            scope = _scope_window(lines, i, max_lines=4)
            hits = find_units_in(scope, allow_low_conf=False)
            if hits:
                return UnitDecision(hits[0][0], "narration")

    # ---- 3) HEADER (issuing-company address) ----
    header = _header_window(page_text)
    hits = find_units_in(header, allow_low_conf=True)
    if hits:
        unique = list(dict.fromkeys(u for u, _ in hits))
        warn = ""
        if len(unique) > 1:
            warn = f"multiple-units-in-header={unique}"
        return UnitDecision(unique[0], "header", warn)

    # ---- 4) ADDRESS / FREE-TEXT FALLBACK ----
    hits = find_units_in(page_text, allow_low_conf=False)
    if hits:
        counts = Counter(u for u, _ in hits)
        most, _ = counts.most_common(1)[0]
        warn = "address-fallback"
        if len(counts) > 1:
            warn += f"; ambiguous={dict(counts)}"
        return UnitDecision(most, "address-fallback", warn)

    return UnitDecision(None, "none", "no-unit-pattern-matched")


# ---------------------------------------------------------------------------
# PDF processing
# ---------------------------------------------------------------------------


def _zip_list() -> list[Path]:
    if not DOWNLOADS.exists():
        return []
    out: list[Path] = []
    for p in DOWNLOADS.iterdir():
        if p.is_file() and any(pat.match(p.name) for pat in ZIP_PATTERNS):
            out.append(p)
    return sorted(out)


def _is_invoice_or_cn_filename(name: str) -> bool:
    if is_cn_filename(name):
        return True
    base = Path(name).name.lower()
    # plain tax-invoice PDFs from APMC zips: '{NN}.pdf' / '{NNNN}.pdf'
    if re.search(r"\{\d+\}\.pdf$", base):
        return True
    if base.endswith(".pdf") and re.search(r"cf53[-_/]2627", base):
        return True
    return False


def _parse_aggregate_pdf(pdf_path: Path) -> list[dict]:
    """Aggregate PDFs contain MANY invoices/CNs concatenated, one per page-group.

    Strategy: parse each page independently. Use `extract_cn_no` to detect the
    document boundary on each page. Carry the most-recent header forward for
    continuation pages (e.g. multi-item invoices that wrap).
    """
    rows: list[dict] = []
    cur_header: dict | None = None
    cur_unit: UnitDecision | None = None

    try:
        pdf = pdfplumber.open(pdf_path)
    except Exception as e:  # noqa: BLE001
        print(f"  ! cannot open {pdf_path.name}: {e!r}")
        return rows

    with pdf:
        for page_idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if not text.strip():
                continue
            cn_no = extract_cn_no(text)
            if cn_no:
                cur_header = {
                    "cn_no": cn_no,
                    "date": extract_date(text),
                    "buyer": extract_buyer(text),
                    "entity": infer_entity(text),
                }
                cur_unit = detect_unit(text)
            else:
                # also support APMC tax-invoice format with 'Invoice No. :CF53/...'
                m = re.search(r"Invoice\s*No\.?\s*:?\s*([A-Z0-9/\-]+)", text)
                if m:
                    cur_header = {
                        "cn_no": m.group(1),
                        "date": extract_date(text),
                        "buyer": extract_buyer(text),
                        "entity": infer_entity(text),
                    }
                    cur_unit = detect_unit(text)
            if cur_header is None or cur_unit is None:
                continue

            items = parse_line_items(text)
            for it in items:
                rows.append(
                    {
                        "cn_no": cur_header["cn_no"],
                        "date": cur_header["date"],
                        "buyer": cur_header["buyer"],
                        "entity": cur_header["entity"],
                        "product": it["product"],
                        "qty_kg": it["qty_kg"] if it["qty_kg"] is not None else "",
                        "hsn": it["hsn"],
                        "amount": it["amount"],
                        "unit": cur_unit.unit or "",
                        "unit_source": cur_unit.source,
                        "unit_warning": cur_unit.warning,
                        "source_zip": "",
                        "source_pdf": f"{pdf_path.name}#p{page_idx+1}",
                    }
                )
    return rows


def _parse_individual_pdf(pdf_path: Path, source_zip: str, member: str) -> list[dict]:
    """For per-invoice PDFs from daily zips, treat the entire document as one."""
    rows: list[dict] = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
            # unit decision uses page-1 text + full doc as fallback
            first_text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception as e:  # noqa: BLE001
        print(f"  ! cannot open {pdf_path.name}: {e!r}")
        return rows

    cn_no = extract_cn_no(full_text)
    if not cn_no:
        # APMC invoices use 'Invoice No. :CF53/...'
        m = re.search(r"Invoice\s*No\.?\s*:?\s*([A-Z0-9/\-]+)", full_text)
        cn_no = m.group(1) if m else None
    if not cn_no:
        return rows

    header = {
        "cn_no": cn_no,
        "date": extract_date(full_text),
        "buyer": extract_buyer(full_text),
        "entity": infer_entity(full_text),
    }
    unit_dec = detect_unit(first_text)
    if unit_dec.unit is None:
        unit_dec = detect_unit(full_text)

    items = parse_line_items(full_text)
    for it in items:
        rows.append(
            {
                "cn_no": header["cn_no"],
                "date": header["date"],
                "buyer": header["buyer"],
                "entity": header["entity"],
                "product": it["product"],
                "qty_kg": it["qty_kg"] if it["qty_kg"] is not None else "",
                "hsn": it["hsn"],
                "amount": it["amount"],
                "unit": unit_dec.unit or "",
                "unit_source": unit_dec.source,
                "unit_warning": unit_dec.warning,
                "source_zip": source_zip,
                "source_pdf": member,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    all_rows: list[dict] = []
    src_counter: Counter[str] = Counter()
    unit_counter: Counter[str] = Counter()
    failures: list[tuple[str, str]] = []

    # 1) Aggregate PDFs in Downloads/Inventory calc/
    for p in AGGREGATE_PDFS:
        if not p.exists():
            print(f"SKIP missing: {p}")
            continue
        print(f"AGG  : {p.name}")
        try:
            rows = _parse_aggregate_pdf(p)
        except Exception as e:  # noqa: BLE001
            failures.append((p.name, repr(e)))
            traceback.print_exc()
            continue
        print(f"  -> {len(rows)} rows")
        for r in rows:
            src_counter[r["unit_source"]] += 1
            unit_counter[r["unit"] or "<none>"] += 1
        all_rows.extend(rows)

    # 2) CN PDFs inside daily zips
    for zip_path in _zip_list():
        try:
            zf = zipfile.ZipFile(zip_path)
        except Exception as e:  # noqa: BLE001
            failures.append((zip_path.name, repr(e)))
            continue
        with zf:
            members = [n for n in zf.namelist() if _is_invoice_or_cn_filename(n)]
            if not members:
                continue
            print(f"ZIP  : {zip_path.name} ({len(members)} pdfs)")
            for member in members:
                safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", member)
                out_pdf = TMP / safe
                try:
                    with zf.open(member) as src, open(out_pdf, "wb") as dst:
                        dst.write(src.read())
                except Exception as e:  # noqa: BLE001
                    failures.append((f"{zip_path.name}::{member}", repr(e)))
                    continue
                try:
                    rows = _parse_individual_pdf(out_pdf, zip_path.name, member)
                except Exception as e:  # noqa: BLE001
                    failures.append((f"{zip_path.name}::{member}", repr(e)))
                    continue
                for r in rows:
                    src_counter[r["unit_source"]] += 1
                    unit_counter[r["unit"] or "<none>"] += 1
                all_rows.extend(rows)

    # 3) Write CSV
    fieldnames = [
        "cn_no",
        "date",
        "buyer",
        "entity",
        "product",
        "qty_kg",
        "hsn",
        "amount",
        "unit",
        "unit_source",
        "unit_warning",
        "source_zip",
        "source_pdf",
    ]
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    # 4) Report
    print()
    print("=" * 60)
    print("UNIT EXTRACTION REPORT")
    print("=" * 60)
    print(f"Rows written : {len(all_rows)}")
    print(f"CSV          : {OUT_CSV}")
    print()
    print("Detection-source distribution:")
    for src, n in src_counter.most_common():
        print(f"  {src:18s} {n:6d}")
    print()
    print("Top units:")
    for u, n in unit_counter.most_common(15):
        print(f"  {u:18s} {n:6d}")
    if failures:
        print()
        print(f"Failures: {len(failures)}")
        for fn, err in failures[:5]:
            print(f"  - {fn}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
