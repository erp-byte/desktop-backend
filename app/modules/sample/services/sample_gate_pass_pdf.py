"""Sample gate-pass PDF (checklist A10 / spec §10).

Extends the existing FPDF approach (mirrors job_card_pdf.JobCardPDF) — no new
PDF library. Renders the Candor gate-pass layout with the sample additions:
Sample Req No, Purpose, NPD badge, converted-from line, a 3-cell signature row
(Business Head / Inventory Manager / Recipient) and a VOIDED watermark.

The PDF is never the source of truth — it is re-rendered from the DB row on each
print (spec §9.4). `gp_data` is the dict from gate_pass_service.get_gate_pass,
optionally augmented with an `items` list (requisition articles).
"""
from __future__ import annotations

from fpdf import FPDF


class SampleGatePassPDF(FPDF):
    def __init__(self, gp: dict):
        super().__init__("P", "mm", "A4")
        self.gp = gp
        self.set_auto_page_break(auto=True, margin=12)

    def _safe(self, text) -> str:
        if text is None:
            return "--"
        s = str(text)
        try:
            s.encode("latin-1")
            return s
        except UnicodeEncodeError:
            return s.encode("latin-1", "replace").decode("latin-1")

    def header(self):
        sd = self.gp.get("sample_details") or {}
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(155, 57, 62)
        title = "CANDOR FOODS - GATE PASS"
        if (sd.get("sample_type") or "") == "NPD":
            title += "   [NPD SAMPLE]"
        self.cell(0, 8, self._safe(title), 0, 1, "C")
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "", 8)
        self.cell(0, 4, "Sample Issuing Gate Pass", 0, 1, "C")
        self.ln(2)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        n = self.gp.get("print_count") or 0
        printed = self.gp.get("last_printed_at") or ""
        self.cell(0, 4,
                  self._safe(f"Printed: {str(printed)[:19]} - Print #{n} - "
                             f"Present this gate pass at security gate"), 0, 1, "C")

    def _kv(self, label, value, w1=45, w2=145):
        self.set_font("Helvetica", "", 8)
        self.cell(w1, 6, self._safe(label), 1, 0)
        self.set_font("Helvetica", "B", 8)
        self.cell(w2, 6, self._safe(value), 1, 1)

    def body(self):
        gp = self.gp
        sd = gp.get("sample_details") or {}
        self._kv("Gate Pass No", gp.get("gate_pass_number"))
        self._kv("Sample Req. No", str(sd.get("requisition_id") or "--"))
        self._kv("Purpose", sd.get("purpose_tag") or sd.get("purpose_note") or "--")
        if sd.get("converted_from_internal"):
            src = sd.get("original_requisition_id")
            self._kv("Converted from Internal Sample", str(src or "--"))
        self._kv("From", gp.get("from_location") or "Candor Foods - Warehouse")
        self._kv("To", self._safe(
            f"{gp.get('recipient_name') or '--'}"
            f"{' (' + gp['recipient_contact'] + ')' if gp.get('recipient_contact') else ''}"))
        if gp.get("driver_name"):
            self._kv("Driver", f"{gp.get('driver_name')} / {gp.get('vehicle_carrier') or '--'}")
        else:
            self._kv("Recipient", gp.get("recipient_name") or "--")
        self.ln(2)

        # Items table
        items = gp.get("items") or []
        self.set_font("Helvetica", "B", 8)
        self.cell(15, 6, "S.No", 1, 0, "C")
        self.cell(110, 6, "Item Description", 1, 0, "C")
        self.cell(30, 6, "Qty", 1, 0, "C")
        self.cell(35, 6, "UOM", 1, 1, "C")
        self.set_font("Helvetica", "", 8)
        for i, it in enumerate(items, 1):
            self.cell(15, 6, str(i), 1, 0, "C")
            self.cell(110, 6, self._safe(it.get("sku_name")), 1, 0)
            qty = it.get("issued_qty") if it.get("issued_qty") is not None else it.get("required_qty")
            self.cell(30, 6, self._safe(qty), 1, 0, "R")
            self.cell(35, 6, self._safe(it.get("uom")), 1, 1, "C")
        if not items:
            self.cell(190, 6, "(no item lines)", 1, 1, "C")
        self.ln(6)

        # 3-cell signature row
        self.set_font("Helvetica", "", 8)
        self.cell(63, 16, "Business Head Sign", 1, 0, "C")
        self.cell(63, 16, "Inventory Manager Sign", 1, 0, "C")
        self.cell(64, 16, "Recipient / Driver Sign", 1, 1, "C")

        if gp.get("voided"):
            self.set_font("Helvetica", "B", 50)
            self.set_text_color(220, 50, 50)
            self.set_xy(20, 120)
            self.cell(170, 20, "VOIDED", 0, 1, "C")
            self.set_text_color(0, 0, 0)
            self.set_font("Helvetica", "", 8)
            self.cell(0, 6, self._safe(f"Void reason: {gp.get('void_reason') or ''}"), 0, 1)


def generate_sample_gate_pass_pdf(gp_data: dict) -> bytes:
    pdf = SampleGatePassPDF(gp_data)
    pdf.add_page()
    pdf.body()
    out = pdf.output()
    return bytes(out)
