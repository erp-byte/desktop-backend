"""Split a PDF into one-page PDFs.

claude_extractor reads a multi-page vendor document one page at a time: a
vendor often sends one scan holding several papers (GST certificate, PAN
card, cancelled cheque, FSSAI licence), and a page read on its own gets the
model's full attention instead of competing with the rest of the file.

Splitting needs `pypdf`. When it is missing, or the PDF is damaged or
password-protected, split_pages() returns None and the caller reads the file
whole, as before.
"""

from __future__ import annotations

import io
import logging
import os

logger = logging.getLogger(__name__)

# Pages read per file. Each page is one Claude call; a long file (an MOA, a
# full company profile) is cut off here and the result says so.
MAX_PAGES_DEFAULT = 30

# Each one-page PDF carries whatever its page uses from the file (fonts,
# images), so the pages add up to more than the file. Some scanners attach
# every page's image to every page; split, such a file would be sent to
# Claude many times over, so above this it is read whole instead.
_SPLIT_GROWTH = 4
_SPLIT_FLOOR_BYTES = 20 * 1024 * 1024


def max_pages() -> int:
    try:
        return max(1, int(os.environ.get("VENDOR_DOC_MAX_PAGES") or MAX_PAGES_DEFAULT))
    except ValueError:
        return MAX_PAGES_DEFAULT


def split_pages(pdf_bytes: bytes, limit: int) -> tuple[list[bytes], int] | None:
    """(the first `limit` pages, each as a PDF of its own, in order; the
    file's page count), or None when the file can't be split."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        logger.warning("vendor.pdf_split.pypdf_missing: reading PDFs whole (pip install pypdf)")
        return None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if reader.is_encrypted and not reader.decrypt(""):
            return None
        total = len(reader.pages)
        budget = max(_SPLIT_GROWTH * len(pdf_bytes), _SPLIT_FLOOR_BYTES)
        pages: list[bytes] = []
        for page in reader.pages[:limit]:
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            pages.append(buf.getvalue())
            budget -= len(pages[-1])
            if budget < 0:
                logger.warning("vendor.pdf_split.too_big pages=%d bytes=%d: reading whole", total, len(pdf_bytes))
                return None
        return pages, total
    except Exception as e:  # noqa: BLE001 - any parse failure means "read it whole"
        logger.warning("vendor.pdf_split.failed err=%r", e)
        return None
