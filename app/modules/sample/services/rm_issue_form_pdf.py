"""RM Issue / Collection Form (Document 015) PDF — NPD plan §10.5.

Mirrors sample_gate_pass_pdf (FPDF; re-rendered from the DB row on each print).
`form` is the dict from rm_issue_form_service.get_form (header + lines). Renders
the live form 1:1 — header, trial details, the RM table (with the Qty-Issued
column the Store fills + ownership), and the two-signature authorisation block.
"""
from __future__ import annotations

from fpdf import FPDF


class RmIssueFormPDF(FPDF):
    def __init__(self, form: dict):
        super().__init__("P", "mm", "A4")
        self.form = form
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
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(155, 57, 62)
        self.cell(0, 7, "CANDOR FOODS - R&D / NPD", 0, 1, "C")
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "B", 10)
        self.cell(0, 6, "Raw Material Issue / Collection Form - NPD Trials", 0, 1, "C")
        self.set_font("Helvetica", "", 7)
        self.cell(0, 4, "Document No. 015", 0, 1, "C")
        self.ln(2)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.cell(0, 4, self._safe(f"Form {self.form.get('form_number')} - Status {self.form.get('status')}"), 0, 1, "C")

    def _kv(self, label, value, w1=45, w2=145):
        self.set_font("Helvetica", "", 8)
        self.cell(w1, 6, self._safe(label), 1, 0)
        self.set_font("Helvetica", "B", 8)
        self.cell(w2, 6, self._safe(value), 1, 1)

    def body(self):
        f = self.form
        self._kv("Form No.", f.get("form_number"))
        self._kv("Trial / Project", f.get("trial_name"))
        self._kv("Product", f.get("product_name"))
        self._kv("Customer", f.get("customer_name") or "Internal Use")
        self._kv("Purpose of Issue", f.get("purpose_tag"))
        self.ln(2)

        # RM table — widths sum to 190mm (A4 usable width).
        self.set_font("Helvetica", "B", 7)
        self.cell(8, 6, "#", 1, 0, "C")
        self.cell(52, 6, "Raw Material", 1, 0, "C")
        self.cell(34, 6, "Location", 1, 0, "C")
        self.cell(22, 6, "Lot No", 1, 0, "C")
        self.cell(20, 6, "Reqd", 1, 0, "C")
        self.cell(20, 6, "Issued", 1, 0, "C")
        self.cell(18, 6, "Own/Cust", 1, 0, "C")
        self.cell(16, 6, "UOM", 1, 1, "C")
        self.set_font("Helvetica", "", 7)
        lines = f.get("lines") or []
        for i, ln in enumerate(lines, 1):
            own = "CUST" if ln.get("ownership") == "CUSTOMER" else "OWN"
            self.cell(8, 6, str(i), 1, 0, "C")
            self.cell(52, 6, self._safe(ln.get("sku_name"))[:40], 1, 0)
            self.cell(34, 6, self._safe(ln.get("location"))[:24], 1, 0)
            self.cell(22, 6, self._safe(ln.get("lot_no"))[:14], 1, 0)
            self.cell(20, 6, self._safe(ln.get("reqd_qty")), 1, 0, "R")
            self.cell(20, 6, self._safe(ln.get("issued_qty")), 1, 0, "R")
            self.cell(18, 6, own, 1, 0, "C")
            self.cell(16, 6, self._safe(ln.get("uom")), 1, 1, "C")
        if not lines:
            self.cell(190, 6, "(no material lines)", 1, 1, "C")
        self.ln(8)

        # Authorisation — the two Document-015 signatures (maker / checker).
        self.set_font("Helvetica", "", 8)
        self.cell(95, 16, "Prepared / Requested By (Name, Sign & Date)", 1, 0, "C")
        self.cell(95, 16, "Issued By - Store (Name, Sign & Date)", 1, 1, "C")

        if f.get("status") == "CANCELLED":
            self.ln(4)
            self.set_text_color(220, 50, 50)
            self.set_font("Helvetica", "B", 10)
            self.cell(0, 6, self._safe(f"CANCELLED: {f.get('cancellation_reason') or ''}"), 0, 1, "C")
            self.set_text_color(0, 0, 0)


def generate_rm_issue_form_pdf(form: dict) -> bytes:
    pdf = RmIssueFormPDF(form)
    pdf.add_page()
    pdf.body()
    # Cross-version: pyfpdf 1.7.2 output(dest="S") returns a latin-1 str (and
    # bare output() would print to stdout); fpdf2 returns a bytearray.
    out = pdf.output(dest="S")
    return out.encode("latin-1") if isinstance(out, str) else bytes(out)
