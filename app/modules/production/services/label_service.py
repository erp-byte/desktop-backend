"""WIP box QR label PDF (Slice 6).

WIP/SFG box label: a 2in x 2in (50.8mm) sticker that is JUST the scannable QR of
the ``box_id`` — no text.
Built with fpdf2 (already a dep) + qrcode (added for this slice). Uses
``output(dest="S")`` and encodes like the existing job_card_pdf renderer.
"""

import io
import logging

import qrcode
from fpdf import FPDF

logger = logging.getLogger(__name__)

# WIP/SFG box sticker is a 2in x 2in square, QR-only label: the QR fills the
# label (less a small print margin for the quiet zone) with no text, so it reads
# cleanly on a small thermal sticker.
_QR_LABEL_MM = 50.8   # 2 inch
_QR_MARGIN_MM = 2.0   # physical print margin around the QR


def _safe(s) -> str:
    """fpdf core fonts are latin-1 only — drop anything they can't encode."""
    return str(s if s is not None else "").encode("latin-1", "replace").decode("latin-1")


def _qr_png(data: str) -> io.BytesIO:
    # qrcode.make() returns a 1-bit ("mode 1") image. fpdf2's PNG decoder
    # mis-handles the per-row bit padding of 1-bit PNGs, which shears the QR into
    # diagonal streaks. Unwrap to the underlying Pillow image and force RGB so
    # fpdf2 gets an unambiguous 8-bit-per-channel raster that prints a clean grid.
    made = qrcode.make(data)                          # qrcode PilImage wrapper
    pil = getattr(made, "_img", None)
    if pil is None:
        pil = made.get_image() if hasattr(made, "get_image") else made
    img = pil.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def wip_box_labels_pdf(boxes: list[dict]) -> bytes:
    """Render one 2in x 2in QR-ONLY sticker per box (no text). ``boxes`` are
    sfg_box rows (dicts).

    Returns PDF bytes (never empty — an empty list still yields a valid 1-page
    'no boxes' sheet so the endpoint contract is simple).
    """
    side = _QR_LABEL_MM
    pdf = FPDF(orientation="P", unit="mm", format=(side, side))
    pdf.set_auto_page_break(auto=False)

    if not boxes:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_xy(0, side / 2 - 4)
        pdf.cell(side, 8, "No SFG boxes", align="C")
        raw = pdf.output(dest="S")
        return raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)

    qr = side - 2 * _QR_MARGIN_MM  # QR fills the label less the print margin
    for b in boxes:
        pdf.add_page()
        box_id = _safe(b.get("box_id"))
        try:
            png = _qr_png(box_id)
            pdf.image(png, x=_QR_MARGIN_MM, y=_QR_MARGIN_MM, w=qr, h=qr)
        except Exception:  # never let a QR-render hiccup 500 the print job
            logger.exception("QR render failed for box %s", box_id)
            pdf.set_xy(_QR_MARGIN_MM, side / 2 - 4)
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(qr, 8, "QR ERR", border=1, align="C")

    raw = pdf.output(dest="S")
    return raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)
