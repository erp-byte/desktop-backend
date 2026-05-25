"""Probe CN/Sales PDFs to inspect ALL text on each page for unit indicators.

Goal: see where each of W-202 / A-185 / F-53 / APMC / COLD STORAGE / RISHI / SAVLA /
A-101 / PAWANE / A-68 actually appears on the page so the extractor can be tuned.
"""
import pdfplumber
import re
from pathlib import Path

BASE = Path(r"C:\Users\cando\Downloads\Inventory calc")
FILES = [
    BASE / "CFPL Credit Notes [From 01st April 2026 To 18th May 2026].pdf",
    BASE / "CDPL Credit Notes [From 01st April 2026 To18th May 2026].pdf",
    BASE / "APMC Sales Invoices [From 01st April 2026 To 18th May 2026].pdf",
]

UNIT_PATTERNS = [
    (re.compile(r"W[- ]?202", re.I), "W-202"),
    (re.compile(r"A[- ]?185", re.I), "A-185"),
    (re.compile(r"F[- ]?53", re.I), "F-53"),
    (re.compile(r"\bAPMC\b", re.I), "APMC(text)"),
    (re.compile(r"A[- ]?68\b", re.I), "A-68"),
    (re.compile(r"cold\s*storage", re.I), "COLD-STORAGE"),
    (re.compile(r"supreme\s*cold", re.I), "SUPREME-COLD"),
    (re.compile(r"\brishi\b", re.I), "RISHI"),
    (re.compile(r"\bsavla\b", re.I), "SAVLA"),
    (re.compile(r"\bD[- ]?39\b", re.I), "D-39"),
    (re.compile(r"\bD[- ]?514\b", re.I), "D-514"),
    (re.compile(r"A[- ]?101\b", re.I), "A-101"),
    (re.compile(r"\bpawane\b", re.I), "PAWANE"),
]

HINTS = [
    "dispatched from", "dispatch doc", "ship from", "consignor",
    "origin", "source", "narration", "remarks", "other references", "notes",
    "terms of delivery",
]

for path in FILES:
    print("=" * 100)
    print("FILE:", path.name)
    if not path.exists():
        print("  MISSING")
        continue
    with pdfplumber.open(path) as pdf:
        npages = len(pdf.pages)
        print(f"  pages={npages}")
        sample_indices = sorted(set([0, 1, 2, npages // 4, npages // 2, npages - 1]))
        for idx in sample_indices:
            if idx < 0 or idx >= npages:
                continue
            text = pdf.pages[idx].extract_text() or ""
            low = text.lower()
            hits = []
            for pat, label in UNIT_PATTERNS:
                for m in pat.finditer(text):
                    s = max(0, m.start() - 40)
                    e = min(len(text), m.end() + 40)
                    snippet = text[s:e].replace("\n", " | ")
                    hits.append((label, m.start(), snippet))
            print(f"\n--- page {idx+1} : unit hits={len(hits)} ---")
            for h in hits[:12]:
                print(f"  [{h[0]:14s}] @{h[1]:5d} ...{h[2]}...")
            # show hint-line lines
            for i, ln in enumerate(text.split("\n")):
                ll = ln.lower()
                if any(h in ll for h in HINTS):
                    print(f"  HINT-LINE {i:3d}: {ln.strip()[:180]}")
