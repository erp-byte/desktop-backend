"""Extract Credit Note (CN) PDFs from daily sales zips and emit a flat CSV.

Output: d:/Consumption/New/Backend/_cn_extracted.csv

Spec source: user message (kaushal). PDFs with 'CN' or 'CND' in filename
are credit notes; plain '{NN}' invoices are skipped. 'Old Invoices/Dmart_CND_*'
files are treated as CNs even though the PDF body says 'Tax Invoice'.
"""
from __future__ import annotations

import csv
import re
import sys
import traceback
import zipfile
from collections import defaultdict
from pathlib import Path

import pdfplumber

DOWNLOADS = Path("C:/Users/cando/Downloads")
TMP = Path("C:/Users/cando/AppData/Local/Temp/cn_extract")
OUT_CSV = Path("d:/Consumption/New/Backend/_cn_extracted.csv")

TMP.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Zip + PDF discovery
# ---------------------------------------------------------------------------

ZIP_PATTERNS = [
    re.compile(r"^\d{6}\.zip$", re.IGNORECASE),
    re.compile(r"^APMC Sales Invoices.*\.zip$", re.IGNORECASE),
]


def list_zips() -> list[Path]:
    out = []
    for p in DOWNLOADS.iterdir():
        if not p.is_file():
            continue
        if any(pat.match(p.name) for pat in ZIP_PATTERNS):
            out.append(p)
    return sorted(out)


def is_cn_filename(name: str) -> bool:
    """Match filenames that contain CN/CND markers.

    Examples:
      - 'ALIGN {CN74}.pdf'      -> True
      - 'D-Mart {CN72}.pdf'     -> True
      - 'Dmart_CND_26-27_811.pdf' -> True
      - 'D-Mart {837}.pdf'      -> False (invoice)
      - 'Tasties [CFPL] {827}.pdf' -> False
    """
    if not name.lower().endswith(".pdf"):
        return False
    base = Path(name).name
    # {CN..} or {CND..} braces marker
    if re.search(r"\{CN[D]?\d+\}", base, re.IGNORECASE):
        return True
    # _CND_ or _CN_ pattern (Old Invoices/Dmart_CND_26-27_811.pdf)
    # use a manual non-letter boundary (\b breaks because _ is a word char)
    if re.search(r"(?:^|[^A-Za-z])CN[D]?_\d", base, re.IGNORECASE):
        return True
    return False



# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

CN_NO_RE = re.compile(
    r"(CF/CN/\d{2}-\d{2}/\d+|CND/\d{2}-\d{2}/\d+)", re.IGNORECASE
)
DATE_RE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{2}")

# Line-item leading regex: starts with a serial number directly followed by
# a non-digit description token. The full row spans several lines, so we use
# a "find the start" + "accumulate until we hit a known sentinel".
ITEM_START_RE = re.compile(r"^(?P<sl>\d{1,3})(?P<rest>[A-Za-z].*)$")
# Anchor: <HSN 8-digit> <GST%> <qty> <unit> <rate> <unit> [disc%] <amount>
ROW_DATA_RE = re.compile(
    r"(?P<hsn>\d{8})\s+(?P<gst>\d+(?:\.\d+)?)\s*%?\s+"
    r"(?P<qty>[\d,]+\.\d+)\s+(?P<unit>Kgs|NOS|Nos|nos|kgs|PCS|Pcs|pcs)\s+"
    r"(?P<rate>[\d,]+\.\d+)\s+(?:Kgs|NOS|Nos|nos|kgs|PCS|Pcs|pcs)"
    r"(?:\s+\d+(?:\.\d+)?\s*%)?\s+"
    r"(?P<amount>[\d,]+\.\d+)\s*$"
)
KGS_PAREN_RE = re.compile(r"\(\s*([\d,]+\.\d+)\s*Kgs\s*\)", re.IGNORECASE)


def to_float(s: str) -> float:
    return float(s.replace(",", ""))


def infer_entity(text: str) -> str:
    head = text[:1500].lower()
    if "candor dates private limited" in head:
        return "CDPL"
    if "candor foods private limited" in head:
        return "CFPL"
    # secondary cues
    if "w-202 (b)" in head or "w-202(b)" in head or "a-185" in head:
        return "CDPL"
    return "CFPL"


def extract_cn_no(text: str) -> str | None:
    m = CN_NO_RE.search(text)
    return m.group(1) if m else None


def extract_date(text: str) -> str | None:
    """First date that appears near the CN number is the doc date.

    The PDFs put the CN number and the date on the same line as the
    header table cell. We look for the first date that follows the
    CN number; fall back to Ack Date / first date.
    """
    m = CN_NO_RE.search(text)
    if m:
        tail = text[m.end(): m.end() + 200]
        d = DATE_RE.search(tail)
        if d:
            return d.group(0)
    d = DATE_RE.search(text)
    return d.group(0) if d else None


# Phrases that should be ignored when collecting the description tail.
DESC_STOP_PHRASES = (
    "cgst output",
    "sgst output",
    "igst (output)",
    "igst output",
    "round off",
    "total",
    "amount chargeable",
    "hsn/sac",
    "company",
    "remarks",
    "tax amount",
    "continued to page",
    "this is a computer",
    "for candor",
    "authorised",
    "declaration",
)


def extract_buyer(text: str) -> str | None:
    """Buyer is the first non-empty line under 'Buyer (Bill to)'."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if "Buyer" in ln and "Bill to" in ln:
            for j in range(i + 1, min(i + 8, len(lines))):
                candidate = lines[j].strip()
                if candidate and not candidate.lower().startswith("gstin"):
                    return candidate
    # Fallback: consignee
    for i, ln in enumerate(lines):
        if "Consignee" in ln and "Ship to" in ln:
            for j in range(i + 1, min(i + 6, len(lines))):
                candidate = lines[j].strip()
                if (
                    candidate
                    and not candidate.lower().startswith("gstin")
                    and not candidate.lower().startswith("dispatch")
                ):
                    return candidate
    return None


def parse_line_items(text: str) -> list[dict]:
    """Parse the goods-table rows.

    Strategy: find each line that matches the ROW_DATA_RE (the data line),
    then walk backwards a few lines to collect the multi-line description
    that begins with a serial number prefix.
    """
    lines = text.splitlines()
    items: list[dict] = []

    for idx, ln in enumerate(lines):
        m = ROW_DATA_RE.search(ln)
        if not m:
            continue
        hsn = m.group("hsn")
        qty_val = to_float(m.group("qty"))
        unit = m.group("unit").lower()
        amount = to_float(m.group("amount"))

        # The data row also begins with "<sl><first-word-of-desc> ..." —
        # capture that as the first description chunk.
        head = ln[: m.start()].strip()
        head_words = []
        sl_match = ITEM_START_RE.match(head)
        if sl_match:
            head_words.append(sl_match.group("rest").strip())
        else:
            head_words.append(head)

        # Continuation lines come AFTER the data row (description wraps
        # below in these PDFs, e.g. "Roasted and Salted" or "(2.4000 Kgs)").
        kg_paren: float | None = None
        tail_desc: list[str] = []
        for j in range(idx + 1, min(idx + 6, len(lines))):
            t = lines[j].strip()
            if not t:
                break
            low = t.lower()
            if any(low.startswith(p) for p in DESC_STOP_PHRASES):
                break
            # next row start?
            if ROW_DATA_RE.search(t):
                break
            # New item rows always start with a serial number followed by
            # an UPPERCASE letter ('1Healthy...', '2Premia...'). A line like
            # '100g (100005226)' is a SIZE qualifier (lowercase) and should
            # be kept as part of the current description.
            if re.match(r"^\d{1,3}[A-Z]", t):
                break
            kg_m = KGS_PAREN_RE.search(t)
            if kg_m:
                kg_paren = to_float(kg_m.group(1))
                continue
            # carton qualifier like "01 Cartn X 24 Units" — skip
            if re.match(r"^\d+\s*Cartn", t, re.IGNORECASE):
                continue
            # Skip pure-numeric continuation
            if re.match(r"^[\d,\.\s]+$", t):
                continue
            tail_desc.append(t)

        description = " ".join(w for w in (head_words + tail_desc) if w).strip()
        description = re.sub(r"\s+", " ", description)

        # Quantity in Kgs: if unit is Kgs, use qty; else use (X Kgs) note.
        if unit in ("kgs",):
            qty_kg: float | None = qty_val
        else:
            qty_kg = kg_paren

        items.append(
            {
                "product": description,
                "qty_kg": qty_kg,
                "hsn": hsn,
                "amount": amount,
            }
        )

    return items


def parse_pdf(pdf_path: Path) -> tuple[dict | None, list[dict], str | None]:
    """Return (header_dict, line_items, error_msg)."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:  # noqa: BLE001
        return None, [], f"pdfplumber-open: {e!r}"

    cn_no = extract_cn_no(text)
    date = extract_date(text)
    buyer = extract_buyer(text)
    entity = infer_entity(text)
    items = parse_line_items(text)

    if not cn_no:
        return None, [], "no cn_no found"
    if not items:
        return (
            {"cn_no": cn_no, "date": date, "buyer": buyer, "entity": entity},
            [],
            "no line items parsed",
        )
    return (
        {"cn_no": cn_no, "date": date, "buyer": buyer, "entity": entity},
        items,
        None,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    zips = list_zips()
    print(f"Found {len(zips)} zip(s):")
    for z in zips:
        print(f"  - {z.name}")

    rows: list[dict] = []
    cn_pdf_count = 0
    parsed_count = 0
    failures: list[tuple[str, str]] = []
    entity_qty: dict[str, float] = defaultdict(float)

    for zip_path in zips:
        try:
            zf = zipfile.ZipFile(zip_path)
        except Exception as e:  # noqa: BLE001
            print(f"  ! cannot open {zip_path.name}: {e!r}")
            continue

        with zf:
            members = [n for n in zf.namelist() if is_cn_filename(n)]
            if not members:
                print(f"  {zip_path.name}: no CN PDFs")
                continue
            print(f"  {zip_path.name}: {len(members)} CN PDF(s)")
            for member in members:
                cn_pdf_count += 1
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", member)
                out_pdf = TMP / safe_name
                try:
                    with zf.open(member) as src, open(out_pdf, "wb") as dst:
                        dst.write(src.read())
                except Exception as e:  # noqa: BLE001
                    failures.append((f"{zip_path.name}::{member}", f"extract: {e!r}"))
                    continue

                try:
                    header, items, err = parse_pdf(out_pdf)
                except Exception as e:  # noqa: BLE001
                    failures.append(
                        (
                            f"{zip_path.name}::{member}",
                            f"parse-exc: {e!r}\n{traceback.format_exc(limit=2)}",
                        )
                    )
                    continue

                if err and not items:
                    failures.append((f"{zip_path.name}::{member}", err))
                    continue

                parsed_count += 1
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
                            "source_zip": zip_path.name,
                            "source_pdf": member,
                        }
                    )
                    if isinstance(it["qty_kg"], (int, float)):
                        entity_qty[header["entity"]] += float(it["qty_kg"])

    # ---- write CSV ----
    fieldnames = [
        "cn_no",
        "date",
        "buyer",
        "entity",
        "product",
        "qty_kg",
        "hsn",
        "amount",
        "source_zip",
        "source_pdf",
    ]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # ---- report ----
    print()
    print("=" * 60)
    print("EXTRACTION REPORT")
    print("=" * 60)
    print(f"Zips processed         : {len(zips)}")
    print(f"CN PDFs found          : {cn_pdf_count}")
    print(f"PDFs successfully parsed: {parsed_count}")
    print(f"Failures               : {len(failures)}")
    print(f"Rows written           : {len(rows)}")
    print(f"CSV path               : {OUT_CSV}")
    print()
    print("Total qty_kg by entity:")
    for ent, q in sorted(entity_qty.items()):
        print(f"  {ent}: {q:,.4f} Kgs")
    print()
    if failures:
        print("Top failures:")
        for fname, reason in failures[:5]:
            short = reason.splitlines()[0]
            print(f"  - {fname}: {short}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
