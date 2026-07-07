"""WIP box QR label PDF (Slice 6).

One label per physical SFG box: a scannable QR encoding the 8-digit ``box_id``,
plus the SFG####, producing job-card number, net weight, floor and box position.
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
_LABEL_W = 105  # mm (A6 landscape width)
_LABEL_H = 74   # mm


def _safe(s) -> str:
    """fpdf core fonts are latin-1 only — drop anything they can't encode."""
    return str(s if s is not None else "").encode("latin-1", "replace").decode("latin-1")


def _qr_png(data: str) -> io.BytesIO:
    img = qrcode.make(data)            # PilImage (Pillow present)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def wip_box_labels_pdf(boxes: list[dict]) -> bytes:
    """Render one QR label per box. ``boxes`` are sfg_box rows (dicts).

    Returns PDF bytes (never empty — an empty list still yields a valid 1-page
    'no boxes' sheet so the endpoint contract is simple).
    """
    pdf = FPDF(orientation="L", unit="mm", format=(_LABEL_H, _LABEL_W))
    pdf.set_auto_page_break(auto=False)

    if not boxes:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 10, "No SFG boxes", border=0)
        raw = pdf.output(dest="S")
        return raw.encode("latin-1") if isinstance(raw, str) else bytes(raw)

    for b in boxes:
        pdf.add_page()
        box_id = _safe(b.get("box_id"))
        # QR on the left.
        try:
            png = _qr_png(box_id)
            pdf.image(png, x=4, y=4, w=46, h=46)
        except Exception:  # never let a QR-render hiccup 500 the print job
            logger.exception("QR render failed for box %s", box_id)
            pdf.set_xy(4, 4)
            pdf.set_font("Helvetica", "", 8)
            pdf.cell(46, 46, "QR ERR", border=1, align="C")

        # Text block on the right.
        x = 54
        pdf.set_xy(x, 5)
        pdf.set_font("Helvetica", "B", 15)
        pdf.cell(0, 8, box_id, ln=1)

        pdf.set_xy(x, 15)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 6, _safe(b.get("sfg_code")), ln=1)

        pdf.set_x(x)
        pdf.set_font("Helvetica", "", 9)
        lines = [
            f"JC  {_safe(b.get('job_card_number'))}",
            f"Net {_safe(b.get('net_weight'))} kg",
            f"{_safe(b.get('entity'))}  ·  {_safe(b.get('floor'))}",
            f"{_safe(b.get('stage_bucket'))}",
        ]
        for ln in lines:
            pdf.set_x(x)
            pdf.cell(0, 5.5, ln, ln=1)

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
