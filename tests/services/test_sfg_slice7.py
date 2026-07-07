"""SFG / Job-Card Slice-7 regression tests (DB-free seams).

Slice 7 is the reporting / PDF / list-contract close-out. The DB-backed
behaviour (a real Stage-2 JC, chain wiring, ingest) is exercised by the
Docker integration harness; these lock in the pure seams that don't need a
live connection:

  1. job_card_pdf.generate_job_card_pdf — a Stage-2 job card's PDF must list
     the SFG (and WIP) input consumption line, labelled, printing the
     SFGxxxx seam code + qty. The renderer is pure (dict in, bytes out), so
     it is driven directly with synthetic detail dicts in BOTH detail
     shapes (v2 `consumption_lines`, v1 `material_consumption`).

  2. list_job_cards / search_job_cards SELECT contract — each returned row
     MUST carry `output_code` and `input_code` (the SFG#### seam codes) so
     the frontend list can surface `SFGxxxx` on Create-WIP rows. The query
     needs a DB to run, so the contract is asserted statically against the
     SELECT source AND functionally through the pass-through serializer.

Runs under pytest (CI) and as a plain script (`python test_sfg_slice7.py`).
"""
from __future__ import annotations

import inspect

from app.modules.production.services import job_card_v2
from app.modules.production.services.job_card_pdf import generate_job_card_pdf


# ── helpers ─────────────────────────────────────────────────────────────────

def _stage2_jc(consumption_key: str = "consumption_lines") -> dict:
    """A minimal Stage-2 (downstream / Final-FG) detail dict: it consumes an
    SFG input plus an RM and a PM line. `consumption_key` lets us exercise
    both the v2 (`consumption_lines`) and v1 (`material_consumption`) shapes.
    """
    consumption = [
        {"material_sku_name": "SALT",    "input_kind": "RM",  "uom": "KGS",
         "issued_qty": 2,   "actual_consumed_qty": 1.9},
        {"material_sku_name": "SFG0042", "input_kind": "SFG", "uom": "KGS",
         "issued_qty": 95,  "actual_consumed_qty": 94.5},
        {"material_sku_name": "WIP0007", "input_kind": "WIP", "uom": "KGS",
         "issued_qty": 10,  "actual_consumed_qty": 9.8},
        {"material_sku_name": "POUCH",   "input_kind": "PM",  "uom": "NOS",
         "issued_qty": 500, "actual_consumed_qty": 498},
    ]
    return {
        "job_card_number": "JC-S7-2",
        "created_at": "2026-06-19T10:00:00",
        "input_code": "SFG0042",
        "output_code": None,
        "section_1_product": {
            "customer_name": "ACME", "fg_sku_name": "Roasted Cashew 200g",
            "article_code": "A1", "batch_size_kg": 100, "quantity_units": 500,
            "sales_order_ref": "SO-1",
        },
        "section_3_team": {"team_leader": "TL", "team_members": ["m1", "m2"]},
        consumption_key: consumption,
        "section_2a_rm_indent": [
            {"material_sku_name": "SALT", "reqd_qty": 2, "issued_qty": 2,
             "batch_no": "B1", "uom": "KGS"},
        ],
        "section_5_output": {
            "fg_actual_units": 498, "fg_actual_kg": 94.0, "net_output_kg": 94.0,
            "yield_pct": 94.0, "rm_consumed_kg": 96.0,
        },
        "material_accounting": {},
        "section_6_signoffs": {},
    }


def _pdf_text(jc: dict, mode: str = "full") -> str:
    """Render with PDF stream compression disabled so the placed text is
    inspectable as latin-1, then return the decoded payload."""
    import app.modules.production.services.job_card_pdf as pdf_mod

    orig_init = pdf_mod.JobCardPDF.__init__

    def _no_compress_init(self, jc_data):
        orig_init(self, jc_data)
        self.set_compression(False)

    pdf_mod.JobCardPDF.__init__ = _no_compress_init
    try:
        raw = pdf_mod.generate_job_card_pdf(jc, mode=mode)
    finally:
        pdf_mod.JobCardPDF.__init__ = orig_init
    return bytes(raw).decode("latin-1", "ignore")


# ── 1. PDF includes the SFG / WIP consumption line ──────────────────────────

def test_pdf_returns_valid_bytes():
    raw = generate_job_card_pdf(_stage2_jc(), mode="full")
    assert isinstance(raw, (bytes, bytearray))
    assert bytes(raw)[:4] == b"%PDF"
    assert len(raw) > 1000


def test_pdf_lists_sfg_input_line_full_mode():
    txt = _pdf_text(_stage2_jc(), mode="full")
    # The SFGxxxx seam code prints, clearly labelled as an SFG input.
    assert "SFG0042" in txt
    assert "[SFG]" in txt


def test_pdf_lists_wip_input_line_full_mode():
    txt = _pdf_text(_stage2_jc(), mode="full")
    assert "WIP0007" in txt
    assert "[WIP]" in txt


def test_pdf_lists_sfg_input_line_bom_mode():
    txt = _pdf_text(_stage2_jc(), mode="bom")
    assert "SFG0042" in txt
    assert "[SFG]" in txt


def test_pdf_reads_v1_material_consumption_key():
    # v1 detail builders hand the renderer `material_consumption`; the SFG
    # line must still print when that legacy key is the one populated.
    txt = _pdf_text(_stage2_jc(consumption_key="material_consumption"), mode="full")
    assert "SFG0042" in txt
    assert "[SFG]" in txt


def test_pdf_stage1_has_no_sfg_line():
    # A Stage-1 (RM-only) JC must NOT sprout a spurious SFG row.
    jc = _stage2_jc()
    jc["consumption_lines"] = [
        {"material_sku_name": "SALT", "input_kind": "RM", "uom": "KGS",
         "issued_qty": 2, "actual_consumed_qty": 1.9},
    ]
    jc["input_code"] = None
    txt = _pdf_text(jc, mode="full")
    assert "[SFG]" not in txt
    assert "[WIP]" not in txt


# ── 2. list / search SELECT returns output_code + input_code ─────────────────

def _list_select_source() -> str:
    return inspect.getsource(job_card_v2.list_job_cards)


def _search_select_source() -> str:
    return inspect.getsource(job_card_v2.search_job_cards)


def test_list_select_includes_seam_codes():
    src = _list_select_source()
    # The contract the frontend depends on: both seam codes are selected so
    # every list row carries them (additive — old fields untouched).
    assert "jc.input_code" in src
    assert "jc.output_code" in src


def test_search_select_includes_seam_codes():
    # Parity: the unpaginated search endpoint returns the same row shape.
    src = _search_select_source()
    assert "jc.input_code" in src
    assert "jc.output_code" in src


def test_serializer_passes_seam_codes_through():
    # _serialize is a pass-through (dict + type coercion). Whatever the
    # SELECT names becomes a result key — so output_code / input_code on a
    # row survive into the JSON response under EXACTLY those names.
    row = {
        "job_card_id": 7, "job_card_number": "JC-7",
        "input_code": "SFG0042", "output_code": "SFG0099",
        "status": "in_progress",
    }
    out = job_card_v2._serialize(row)
    assert out["input_code"] == "SFG0042"
    assert out["output_code"] == "SFG0099"


def test_create_wip_row_exposes_output_code():
    # A Create-WIP / intermediate stage PRODUCES an SFG: output_code is the
    # value the list UI renders as `SFGxxxx`. input_code is None there.
    row = {"job_card_id": 1, "input_code": None, "output_code": "SFG0042"}
    out = job_card_v2._serialize(row)
    assert out["output_code"] == "SFG0042"
    assert out["input_code"] is None


if __name__ == "__main__":  # plain-script runner (no pytest needed)
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} SFG Slice-7 unit tests passed")
