"""
Generate a combined PDF of all DB schema PlantUML diagrams.

Usage:
    python scripts/generate_db_diagrams_pdf.py

Requirements (already installed):
    fpdf2, pillow, requests
"""

import zlib
import base64
import urllib.request
import urllib.error
import tempfile
import os
import sys
import time
from pathlib import Path
from fpdf import FPDF
from PIL import Image


# ── PlantUML encoding for plantuml.com ───────────────────────────────────────

_PU_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"

def _enc6(b):
    return _PU_CHARS[b & 0x3F]

def _enc3bytes(b1, b2, b3):
    return (_enc6(b1 >> 2) + _enc6(((b1 & 3) << 4) | (b2 >> 4)) +
            _enc6(((b2 & 0xF) << 2) | (b3 >> 6)) + _enc6(b3 & 0x3F))

def plantuml_encode(text):
    data = zlib.compress(text.encode("utf-8"), 9)
    result, i = [], 0
    while i < len(data):
        b1 = data[i]
        b2 = data[i+1] if i+1 < len(data) else 0
        b3 = data[i+2] if i+2 < len(data) else 0
        result.append(_enc3bytes(b1, b2, b3))
        i += 3
    return "".join(result)


# ── Kroki encoding (deflate + base64url) ─────────────────────────────────────

def kroki_encode(text):
    compressed = zlib.compress(text.encode("utf-8"), 9)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


# ── fetch PNG (tries multiple servers) ───────────────────────────────────────

SERVERS = [
    # kroki.io — reliable, no rate limit issues
    lambda text: f"https://kroki.io/plantuml/png/{kroki_encode(text)}",
    # plantuml.com as fallback
    lambda text: f"https://www.plantuml.com/plantuml/png/{plantuml_encode(text)}",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DB-Diagram-Generator/1.0)",
    "Accept": "image/png",
}


def fetch_png(puml_text, retries=3, delay=2.0):
    last_err = None
    for url_fn in SERVERS:
        url = url_fn(puml_text)
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                    if len(data) < 200:
                        raise ValueError("Response too small, likely an error image")
                    return data
            except Exception as e:
                last_err = e
                print(f"    [{url[:50]}...] attempt {attempt+1}: {e}")
                time.sleep(delay)
    raise RuntimeError(f"All servers failed. Last error: {last_err}")


# ── safe ASCII for FPDF core fonts ───────────────────────────────────────────

def safe(text):
    replacements = {
        "\u2014": "-", "\u2013": "-", "\u2019": "'", "\u2018": "'",
        "\u201c": '"', "\u201d": '"', "\u2192": "->", "\u2190": "<-",
        "\u2026": "...", "\u00e9": "e", "\u00e8": "e", "\u00ea": "e",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


# ── PDF ───────────────────────────────────────────────────────────────────────

class DiagramPDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, "Candor Foods - Production Management System", align="C")
        self.ln(2)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, f"Page {self.page_no()}", align="C")


def add_diagram_page(pdf, title, png_bytes, tmp_dir):
    tmp_path = os.path.join(tmp_dir, "diagram.png")
    with open(tmp_path, "wb") as f:
        f.write(png_bytes)

    with Image.open(tmp_path) as img:
        img_w, img_h = img.size

    # A3 landscape
    page_w, page_h = 420, 297
    margin = 15
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin - 14

    # 96 dpi: 1 inch = 96 px = 25.4 mm  =>  1 px = 25.4/96 mm
    px_to_mm = 25.4 / 96
    img_w_mm = img_w * px_to_mm
    img_h_mm = img_h * px_to_mm

    scale = min(usable_w / img_w_mm, usable_h / img_h_mm, 1.0)
    draw_w = img_w_mm * scale
    draw_h = img_h_mm * scale

    x = margin + (usable_w - draw_w) / 2
    y = margin + 14

    pdf.add_page(orientation="L", format="A3")
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(0, 51, 102)
    pdf.set_xy(margin, margin + 7)
    pdf.cell(0, 6, safe(title))
    pdf.set_text_color(0, 0, 0)
    pdf.image(tmp_path, x=x, y=y, w=draw_w, h=draw_h)


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    base = Path(__file__).parent.parent
    puml_dir = base / "docs" / "db" / "puml"
    out_path = base / "docs" / "db" / "DB_Schema_Diagrams.pdf"

    puml_files = sorted(puml_dir.glob("*.puml"))
    if not puml_files:
        print(f"No .puml files in {puml_dir}")
        sys.exit(1)

    print(f"Found {len(puml_files)} diagram(s)\n")

    pdf = DiagramPDF()
    pdf.set_auto_page_break(auto=False)

    # Cover page
    pdf.add_page(format="A3", orientation="L")
    # Background band
    pdf.set_fill_color(58, 90, 140)
    pdf.rect(0, 90, 420, 80, "F")
    pdf.set_font("Helvetica", "B", 32)
    pdf.set_text_color(255, 255, 255)
    pdf.set_y(112)
    pdf.cell(0, 16, "Production Management System", align="C")
    pdf.ln(14)
    pdf.set_font("Helvetica", "", 17)
    pdf.set_text_color(200, 215, 240)
    pdf.cell(0, 10, "Module and Data Flow Overview", align="C")
    pdf.set_y(185)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(58, 90, 140)
    pdf.cell(0, 7, "Candor Foods", align="C")
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(0, 6, f"Prepared: {time.strftime('%d %b %Y')}", align="C")

    failed = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for puml_file in puml_files:
            stem = puml_file.stem
            parts = stem.split("_", 1)
            num = parts[0]
            label = parts[1].replace("_", " ").title() if len(parts) > 1 else stem
            title = f"{num}. {label}"

            print(f"  Rendering: {title} ...", end=" ", flush=True)
            puml_text = puml_file.read_text(encoding="utf-8")

            try:
                png_bytes = fetch_png(puml_text)
                add_diagram_page(pdf, title, png_bytes, tmp_dir)
                print("OK")
            except Exception as e:
                print(f"FAILED ({e})")
                failed.append(title)
                pdf.add_page(format="A3", orientation="L")
                pdf.set_font("Helvetica", "B", 14)
                pdf.set_text_color(180, 0, 0)
                pdf.set_y(130)
                pdf.cell(0, 10, safe(f"{title} - could not render"), align="C")
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(100, 100, 100)
                pdf.ln(8)
                pdf.cell(0, 6, safe(str(e))[:120], align="C")

            time.sleep(0.4)

    pdf.output(str(out_path))
    print(f"\nSaved: {out_path}  ({pdf.page_no()} pages)")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}")


if __name__ == "__main__":
    main()
