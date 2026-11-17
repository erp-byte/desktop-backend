"""Vendor documents read one page at a time (claude_extractor, pdf_pages).

A multi-page PDF is split into one-page PDFs; each page goes to Claude on its
own, several at a time, and the pages' fields are merged into the file's
extraction, with what each page gave kept in `pages`. These tests stand a fake
in for the Anthropic client, so the real split, prompt, parse and merge run.
"""
import asyncio
import base64
import io
import json
import sys

import pytest
from pypdf import PdfReader, PdfWriter

from app.modules.vendor.schemas import ExtractedDocFields
from app.modules.vendor.services import claude_extractor as CE
from app.modules.vendor.services import extraction_service as ES
from app.modules.vendor.services import pdf_pages


def _pdf(n: int) -> bytes:
    """n blank pages; page i is 100 + i points wide, so a page sent on its own
    says which page it was."""
    w = PdfWriter()
    for i in range(1, n + 1):
        w.add_blank_page(width=100 + i, height=100)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _which_page(pdf: bytes) -> int:
    """The page a one-page PDF was cut from; 0 for a file sent whole."""
    r = PdfReader(io.BytesIO(pdf))
    return int(float(r.pages[0].mediabox.width)) - 100 if len(r.pages) == 1 else 0


class _FakeClaude:
    """Answers each call with the JSON (or raises the error) given for the
    page it was sent; key 0 is a file sent whole, or an image."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[dict] = []
        self.in_flight = 0
        self.peak = 0
        self.messages = self

    async def create(self, *, model, max_tokens, system, messages):
        file_block, text_block = messages[0]["content"]
        data = base64.b64decode(file_block["source"]["data"])
        page = _which_page(data) if file_block["type"] == "document" else 0
        self.calls.append({"page": page, "prompt": text_block["text"], "bytes": data})
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            self.in_flight -= 1
        answer = self.answers.get(page, {})
        if isinstance(answer, Exception):
            raise answer

        class _Text:
            type = "text"
            text = json.dumps(answer)

        class _Response:
            content = [_Text()]

        return _Response()


@pytest.fixture
def claude(monkeypatch):
    def install(answers):
        fake = _FakeClaude(answers)
        monkeypatch.setattr(CE, "_client", lambda: fake)
        return fake
    return install


def _extract(data: bytes, doc_type="OTHER", mime="application/pdf") -> ExtractedDocFields:
    return asyncio.run(CE.extract_document_fields(file_bytes=data, mime_type=mime, doc_type=doc_type))


PAN_PAGE = {"doc_number": "AAACI1234A", "pan": "AAACI1234A", "holder_name": "RAJESH KUMAR"}
GST_PAGE = {"doc_number": "27AAACI1234A1Z5", "gstin": "27AAACI1234A1Z5", "pan": "AAACI1234A",
            "business_name": "Acme Foods Pvt Ltd", "address": "12 MG Road, Pune"}
CHEQUE_PAGE = {"bank_name": "HDFC Bank", "bank_account_no": "50100012345678", "bank_ifsc": "HDFC0000123",
               "additional_notes": "Cancelled cheque."}


# ── splitting ────────────────────────────────────────────────────────────


def test_split_gives_each_page_as_its_own_pdf_in_order():
    pages, total = pdf_pages.split_pages(_pdf(3), 30)
    assert total == 3 and [_which_page(p) for p in pages] == [1, 2, 3]


def test_split_stops_at_the_limit_but_counts_every_page():
    pages, total = pdf_pages.split_pages(_pdf(5), 2)
    assert total == 5 and [_which_page(p) for p in pages] == [1, 2]


def test_a_file_that_is_not_a_pdf_is_not_split():
    assert pdf_pages.split_pages(b"%PDF-1.4 but not really", 30) is None


def test_a_file_whose_pages_would_balloon_is_not_split(monkeypatch):
    """Pages that together come to far more than the file (every page
    carrying every scan) would send the file to Claude many times over."""
    monkeypatch.setattr(pdf_pages, "_SPLIT_FLOOR_BYTES", 0)
    monkeypatch.setattr(pdf_pages, "_SPLIT_GROWTH", 1)       # pages may not add up to more than the file
    assert pdf_pages.split_pages(_pdf(3), 30) is None
    monkeypatch.setattr(pdf_pages, "_SPLIT_GROWTH", 4)
    assert pdf_pages.split_pages(_pdf(3), 30) is not None


def test_without_pypdf_nothing_is_split(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)     # import pypdf raises ImportError
    assert pdf_pages.split_pages(_pdf(3), 30) is None


# ── extraction page by page ──────────────────────────────────────────────


def test_each_page_is_read_alone_and_the_pages_merged(claude):
    fake = claude({1: PAN_PAGE, 2: GST_PAGE, 3: CHEQUE_PAGE})
    ext = _extract(_pdf(3))
    assert sorted(c["page"] for c in fake.calls) == [1, 2, 3]
    for c in fake.calls:
        assert f"This is page {c['page']} of 3 of the uploaded file" in c["prompt"]
    assert ext.extraction_status == "ok"
    assert (ext.pan, ext.gstin, ext.holder_name, ext.business_name) == (
        "AAACI1234A", "27AAACI1234A1Z5", "RAJESH KUMAR", "Acme Foods Pvt Ltd")
    assert (ext.bank_name, ext.bank_account_no, ext.bank_ifsc) == ("HDFC Bank", "50100012345678", "HDFC0000123")
    assert ext.doc_number == "AAACI1234A"               # auto-detect: the first page's number
    assert ext.additional_notes == "Page 3: Cancelled cheque."
    assert [p["page"] for p in ext.pages] == [1, 2, 3]
    assert ext.pages[1]["gstin"] == "27AAACI1234A1Z5" and "bank_name" not in ext.pages[1]


def test_a_declared_type_takes_its_own_number_from_whichever_page(claude):
    claude({1: PAN_PAGE, 2: GST_PAGE})
    assert _extract(_pdf(2), doc_type="GST").doc_number == "27AAACI1234A1Z5"
    assert _extract(_pdf(2), doc_type="PAN").doc_number == "AAACI1234A"


def test_a_declared_number_missing_from_doc_number_still_comes_from_its_slot(claude):
    claude({1: PAN_PAGE, 2: {"gstin": "27AAACI1234A1Z5"}})
    assert _extract(_pdf(2), doc_type="GST").doc_number == "27AAACI1234A1Z5"


def test_a_page_that_fails_is_noted_and_the_rest_still_count(claude):
    claude({1: GST_PAGE, 2: RuntimeError("overloaded")})
    ext = _extract(_pdf(2))
    assert ext.extraction_status == "ok" and ext.gstin == "27AAACI1234A1Z5"
    assert "Page 2 could not be read: RuntimeError('overloaded')" in ext.additional_notes
    assert ext.pages[1]["extraction_status"] == "failed"


def test_the_file_fails_only_when_every_page_failed(claude):
    claude({1: RuntimeError("a"), 2: RuntimeError("b")})
    ext = _extract(_pdf(2))
    assert ext.extraction_status == "failed"
    assert ext.extraction_error.startswith("none of the 2 pages could be read")
    assert len(ext.pages) == 2


def test_pages_past_the_limit_are_reported_not_dropped_silently(claude, monkeypatch):
    monkeypatch.setenv("VENDOR_DOC_MAX_PAGES", "2")
    fake = claude({1: GST_PAGE})
    ext = _extract(_pdf(4))
    assert sorted(c["page"] for c in fake.calls) == [1, 2]
    assert "Only pages 1-2 of 4 were read" in ext.additional_notes


def test_calls_in_flight_stay_under_the_cap(claude, monkeypatch):
    monkeypatch.setenv("VENDOR_DOC_CONCURRENCY", "2")
    fake = claude({})
    _extract(_pdf(6))
    assert len(fake.calls) == 6 and fake.peak == 2


def test_a_one_page_pdf_an_image_and_an_unsplittable_pdf_go_whole(claude, monkeypatch):
    fake = claude({1: GST_PAGE, 0: {"pan": "AAACI1234A", "pages": "the model's, ignored"}})
    one = _pdf(1)
    ext = _extract(one)
    assert fake.calls[-1]["bytes"] == one and "This is page" not in fake.calls[-1]["prompt"]
    assert ext.gstin == "27AAACI1234A1Z5" and ext.pages is None

    ext = _extract(b"\x89PNG fake", mime="image/png")
    assert fake.calls[-1]["page"] == 0 and ext.pan == "AAACI1234A" and ext.pages is None

    monkeypatch.setattr(pdf_pages, "split_pages", lambda data, limit: None)
    three = _pdf(3)
    ext = _extract(three)
    assert fake.calls[-1]["bytes"] == three and ext.pages is None and len(fake.calls) == 3


# ── onboarding aggregation uses each page ────────────────────────────────


def _merged(*pages, doc_type="OTHER"):
    return CE.merge_page_results([ExtractedDocFields(**p) for p in pages], doc_type, len(pages))


def test_pages_that_disagree_are_a_conflict_named_by_page():
    ext = _merged(GST_PAGE, {**GST_PAGE, "gstin": "27AAACI9999A1Z5", "doc_number": "27AAACI9999A1Z5"})
    vendor, _, docs, conflicts, _ = ES._aggregate([("s3://a.pdf", "GST", ext)])
    gst = next(c for c in conflicts if c["field"] == "gstn")
    assert gst["values_seen"] == ["27AAACI1234A1Z5", "27AAACI9999A1Z5"]
    assert gst["sources"] == ["GST page 1", "GST page 2"]
    assert vendor["gstn"] == "27AAACI1234A1Z5"
    assert [p["page"] for p in docs[0]["pages"]] == [1, 2]


def test_an_account_on_every_page_is_one_bank_row_and_a_second_account_another():
    other = {**CHEQUE_PAGE, "bank_account_no": "999888777", "bank_ifsc": "SBIN0000001"}
    footer = {**CHEQUE_PAGE, "bank_account_no": "5010 0012 345678"}     # same account, spaced
    bank_named_only = {"bank_name": "HDFC Bank"}
    ext = _merged(GST_PAGE, bank_named_only, CHEQUE_PAGE, footer, other)
    _, banks, _, _, _ = ES._aggregate([("s3://a.pdf", "OTHER", ext)])
    assert [b["account_no"] for b in banks] == ["50100012345678", "999888777"]

    ext = _merged(GST_PAGE, bank_named_only)            # no account anywhere: the bank alone still shows
    _, banks, _, _, _ = ES._aggregate([("s3://a.pdf", "OTHER", ext)])
    assert [(b["bank_name"], b["account_no"]) for b in banks] == [("HDFC Bank", "")]


def test_a_file_read_whole_aggregates_as_before():
    ext = ExtractedDocFields(**GST_PAGE, **{k: v for k, v in CHEQUE_PAGE.items() if k != "additional_notes"})
    vendor, banks, docs, conflicts, _ = ES._aggregate([("s3://a.pdf", "GST", ext)])
    assert vendor["gstn"] == "27AAACI1234A1Z5" and len(banks) == 1
    assert docs[0]["pages"] is None and conflicts == []
