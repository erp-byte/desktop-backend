"""WIP box QR label PDF (Slice 6).

WIP/SFG box label: a 2in x 2in (50.8mm) sticker that is JUST the scannable QR of
the ``box_id`` — no text. FG carton stickers (below) still carry the text block.
Built with fpdf2 (already a dep) + qrcode (added for this slice). Uses
``output(dest="S")`` and encodes like the existing job_card_pdf renderer.
"""

import io
import logging

import qrcode
from fpdf import FPDF

logger = logging.getLogger(__name__)

# A6-ish label, two per A5 row would need layout work; one label per page keeps
# it simple and printer-agnostic (each label is a full small page).
_LABEL_W = 105  # mm (A6 landscape width) — FG carton text label
_LABEL_H = 74   # mm

# WIP/SFG box sticker is a 2in x 2in square, QR-only label: the QR fills the
# label (less a small print margin for the quiet zone) with no text, so it reads
# cleanly on a small thermal sticker. FG cartons keep the larger text label.
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


# ── FG carton stickers (item_type='fg') ────────────────────────────────────

def _carton_id_of(c: dict) -> str:
    cid = c.get("carton_id")
    return _safe(cid if cid is not None else c.get("box_id"))


def _fg_carton_page(pdf: FPDF, c: dict) -> None:
    """Render one FG carton sticker page: QR(carton_id) + FG name + JC + batch +
    net kg + units + entity/floor."""
    pdf.add_page()
    cid = _carton_id_of(c)
    try:
        png = _qr_png(cid)
        pdf.image(png, x=4, y=4, w=46, h=46)
    except Exception:  # never let a QR-render hiccup 500 the print job
        logger.exception("QR render failed for carton %s", cid)
        pdf.set_xy(4, 4)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(46, 46, "QR ERR", border=1, align="C")

    x = 54
    pdf.set_xy(x, 5)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, cid, ln=1)

    pdf.set_xy(x, 15)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, _safe(c.get("fg_sku_name") or c.get("sfg_code")), ln=1)

    net = c.get("net_weight_kg")
    if net is None:
        net = c.get("net_weight")
    pdf.set_x(x)
    pdf.set_font("Helvetica", "", 9)
    lines = [
        f"JC  {_safe(c.get('job_card_number'))}",
        f"Batch {_safe(c.get('batch_code'))}",
        f"Net {_safe(net)} kg",
        f"Units {_safe(c.get('units'))}",
        f"{_safe(c.get('entity'))}  ·  {_safe(c.get('floor'))}",
    ]
    for ln in lines:
        pdf.set_x(x)
        pdf.cell(0, 5.5, ln, ln=1)


def fg_carton_labels_pdf(cartons: list[dict]) -> bytes:
    """Render one QR sticker per FG carton (``cartons`` are sfg_box item_type='fg'
    rows). Always returns valid PDF bytes — an empty list yields a 'no cartons'
    sheet so the endpoint contract stays simple."""
    pdf = FPDF(orientation="L", unit="mm", format=(_LABEL_H, _LABEL_W))
    pdf.set_auto_page_break(auto=False)

    if not cartons:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 10, "No FG cartons", border=0)
        raw = pdf.output(dest="S")
        return raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)

    for c in cartons:
        _fg_carton_page(pdf, c)

    raw = pdf.output(dest="S")
    return raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)


def fg_carton_label_pdf(carton: dict) -> bytes:
    """Single FG carton sticker (one page)."""
    return fg_carton_labels_pdf([carton])
