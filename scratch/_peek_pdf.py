"""Quick peek script — dump first page text of selected PDFs."""
import pdfplumber, sys
for p in sys.argv[1:]:
    print(f"===== {p} =====")
    try:
        with pdfplumber.open(p) as pdf:
            for i, page in enumerate(pdf.pages[:1]):
                txt = page.extract_text() or ""
                for ln in txt.split("\n")[:40]:
                    print(ln)
    except Exception as e:
        print("ERR", e)
    print()
