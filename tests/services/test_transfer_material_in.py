"""ERP Material In boxes (po_box) on Scan & send, read only, against the SQL-evaluating fake
database of test_transfer_box_lookup_cold (with the FOR UPDATE / ORDER BY / request-lock
support of test_transfer_dispatch_lock and the receipt support of test_transfer_record_only),
plus material_in_source's two statements, which that fake cannot read: MaterialInDB answers
them from its po_* / all_sku tables as Postgres does. (The statements themselves were run in
an in-process Postgres by the material-in probe.)

What is pinned:
  - IMS first: the IMS UNION is sent as it was, with Material In there or not, and an IMS
    hit sends nothing to Material In, not even a probe; a Material In box is found only
    after the UNION misses (an IMS row with the same key wins); the "BE-" path never reads
    it; without the po tables a miss is the 404 it was, after one probe (po_box), cached;
  - the payload: an IMS hit's keys and types plus source / company / inward_site /
    po_number / tally_name / article_basis; the lot is the box's, else its section's
    (trimmed); box_number is the box's number in its lot section; NULL weights are 0.0;
    box id and transaction are matched trimmed (the UNION gets them as sent);
  - 409: a deleted PO (lookups and a request dispatch), a box number more than one box of
    the PO carries (manual entry; a number one box carries is found), a lot longer than 50
    or an article longer than 255 characters;
  - resolve_article under each FUZZY_NAMES (set to "always"): the exact item-master name
    (case and spaces ignored, never semi-finished) first, then the matched name at a score
    of 0.70 or more, else the Tally name; the neighbouring grades pass only "always";
  - R1: a request dispatch judges and stores each Material In box by its Material In
    record, and refuses one the request does not ask for;
  - park_boxes: a warehouse copy of the po_box row (source_table 'po_box', no source_row_id
    or cold snapshot) under the PO's company, from any site; at a cold From site never a
    copy of an IMS cold row sharing its box_id; IMS boxes park exactly as they did, also
    when a Material In box has their very key (the lookups find IMS's row, so it is the one
    copied, from any site, on a backfill and on a receipt undone too); a receipt undone
    re-parks a Material In box from po_box (strict_cold), its PO deleted since or not; the
    backfill copies it too;
  - an edit keeps its copy and reads po_box only for a box it adds; a weight edit tells whose
    weight the copy holds by the po_box row while the two agree (_reweighed: a box Material
    In weighed since its dispatch takes the new weight); scans, removals and weight edits of
    Material In boxes leave what create_transfer writes (INVARIANT S);
  - one box per box id on a transfer: POST / PUT /transfers and a later scan refuse a box
    whose box id a box of another transaction has there (409 naming both transactions);
  - record only: po_header / po_line / po_section / po_box / all_sku / inventory_batch rows
    as they were, and no INSERT / UPDATE / DELETE on them or on a stock table.

Run:  PYTHONPATH=. python -m pytest tests/services/test_transfer_material_in.py
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.modules.transfer import schemas
from app.modules.transfer.services import create_service, delete_service, lookup_service
from app.modules.transfer.services import material_in_source as mis
from app.modules.transfer.services import query_service, receive_service, reversal_service, scan_service
from tests.services import test_transfer_box_lookup_cold as cold
from tests.services import test_transfer_box_rules as br
from tests.services import test_transfer_dispatch_lock as dl
from tests.services import test_transfer_record_only as ro
from tests.services import test_transfer_scan_save as ss

USER, TNO, REQ = dl.USER, dl.TNO, ss.REQ
PO_TABLES = ("po_header", "po_line", "po_section", "po_box")
MATERIAL_IN = (*PO_TABLES, "all_sku", "inventory_batch")


@pytest.fixture(autouse=True)
def _fresh_existence_cache(monkeypatch):
    # lookup_service caches to_regclass answers for the process (material_in_source's too).
    monkeypatch.setattr(lookup_service, "_EXISTS", {})


@pytest.fixture(autouse=True)
def no_readback(monkeypatch):
    async def get_transfer(conn, transfer_id):
        return {"id": transfer_id}
    monkeypatch.setattr(query_service, "get_transfer", get_transfer)


# ── The fake database ────────────────────────────────────────────────────────
_PO_BOX_SQL = {col: " ".join(mis._BOX_SQL.format(match=match).split()) for col, match in mis._MATCH.items()}
_PO_SKU_SQL = " ".join(mis._EXACT_SKU_SQL.split())


class MaterialInDB(ro.ReceiveDB):
    """The transfer fakes' database plus material_in_source's two statements, answered from
    the po_* and all_sku tables as Postgres answers them: the box statement joins po_box to
    po_header (inner), po_line and po_section (left), matches box_id or box_number and the
    transaction, orders by line, section and box_id, and returns two rows at most; the
    item-master statement matches particulars with whitespace collapsed in upper case, never
    a semi-finished (sfg) row, rm / pm / fg first, then the lowest sku_id, its uom as text."""

    def _run(self, kind, sql, args):
        s = " ".join(sql.split())
        col = next((c for c, text in _PO_BOX_SQL.items() if s == text), None)
        if col is not None:
            self.calls.append((kind, s, args))
            return self._po_boxes(col, *args), None
        if s == _PO_SKU_SQL:
            self.calls.append((kind, s, args))
            return self._master(*args), None
        return super()._run(kind, sql, args)

    def _po_boxes(self, col, value, tno):
        heads = {h["transaction_no"]: h for h in self._table("po_header")}
        lines = {(l["transaction_no"], l["line_number"]): l for l in self._table("po_line")}
        lots = {(s["transaction_no"], s["line_number"], s["section_number"]): s for s in self._table("po_section")}
        rows = []
        for b in self._table("po_box"):
            h = heads.get(b["transaction_no"])
            if h is None or b[col] != value or b["transaction_no"] != tno:
                continue
            line = lines.get((b["transaction_no"], b["line_number"]), {})
            lot = lots.get((b["transaction_no"], b["line_number"], b["section_number"]), {})
            rows.append({
                **{c: b[c] for c in ("box_id", "transaction_no", "line_number", "section_number", "box_number",
                                     "net_weight", "gross_weight")},
                "box_lot": b["lot_number"], "section_lot": lot.get("lot_number"),
                **{c: h.get(c) for c in ("entity", "warehouse", "po_number", "deleted_at")},
                **{c: line.get(c) for c in ("sku_name", "particulars", "match_score", "item_category",
                                            "sub_category", "item_type", "uom")},
            })
        return sorted(rows, key=lambda r: (r["line_number"], r["section_number"], r["box_id"]))[:2]

    def _master(self, key):
        hits = sorted((a for a in self._table("all_sku")
                       if " ".join(a["particulars"].split()).upper() == key
                       and (a["item_type"] or "").lower() != "sfg"),
                      key=lambda a: ((a["item_type"] or "").lower() not in ("rm", "pm", "fg"), a["sku_id"]))
        return [{"particulars": a["particulars"], "item_type": a["item_type"], "item_group": a["item_group"],
                 "sub_group": a["sub_group"], "uom": None if a["uom"] is None else str(a["uom"])}
                for a in hits[:1]]

    def po_reads(self, start: int = 0) -> list:
        """The Material In box statements sent from call `start` on: (id column, args)."""
        by_text = {text: col for col, text in _PO_BOX_SQL.items()}
        return [(by_text[s], a) for _, s, a in self.calls[start:] if s in by_text]


# ── Material In ──────────────────────────────────────────────────────────────
PO, PO_NO = "TR-20261016093000", "PO/2026/0118"            # a Material In PO: its QR's "tx"
MI_A1, MI_A2, MI_A3 = "71000001-1", "71000001-2", "71000001-3"   # almond: #1 of LOT-9, #1 of LOT-7, #2 of LOT-9
MI_C, MI_P = "71000001-4", "71000001-5"                     # cashew (L-3); pista (no item-master name)


def _po_box(box_id, line, section, number, *, net=None, gross=None, lot=None, tno=PO) -> dict:
    return {"box_id": box_id, "transaction_no": tno, "line_number": line, "section_number": section,
            "box_number": number, "net_weight": None if net is None else Decimal(net),
            "gross_weight": None if gross is None else Decimal(gross), "lot_number": lot, "count": None,
            "created_at": datetime(2026, 10, 16, 9, 30)}


def _material_in(*, entity="cfpl", warehouse="Warehouse A185", deleted_at=None) -> dict:
    """PO `PO` as Material In keeps it, received into `warehouse` for `entity`:
      line 1, Tally name 'almond  25kg' (the item master's 'ALMOND 25KG' with case and spaces
        ignored; matched, below the floor, to 'ALMOND 25 KG'), in lot sections LOT-9 (boxes
        #1 MI_A1 and #2 MI_A3, the latter with its own padded lot and no weights) and LOT-7
        (box #1 MI_A2, no gross weight);
      line 2, Tally name 'Cashew W-320 Premium', matched to 'CASHEW W320' at 0.82, lot L-3 (#1 MI_C);
      line 3, Tally name 'Pista Kernel', unmatched, lot LOT-1 (#1 MI_P).
    The item master also has a semi-finished 'almond 25kg', which never names a box. Material
    In's inventory_batch copy of MI_A1 is there to show it is never written."""
    return {
        "po_header": [{"transaction_no": PO, "entity": entity, "po_number": PO_NO, "warehouse": warehouse,
                       "deleted_at": deleted_at, "status": "pending"}],
        "po_line": [
            {"transaction_no": PO, "line_number": 1, "sku_name": "almond  25kg", "particulars": "ALMOND 25 KG",
             "match_score": Decimal("0.640"), "item_category": "DRY FRUIT", "sub_category": "ALMONDS",
             "item_type": "rm", "uom": "25.0"},
            {"transaction_no": PO, "line_number": 2, "sku_name": "Cashew W-320 Premium", "particulars": "CASHEW W320",
             "match_score": Decimal("0.820"), "item_category": "NUTS", "sub_category": "CASHEW", "item_type": "rm",
             "uom": "10.0"},
            {"transaction_no": PO, "line_number": 3, "sku_name": "Pista Kernel", "particulars": None,
             "match_score": None, "item_category": None, "sub_category": None, "item_type": None, "uom": None},
        ],
        "po_section": [
            {"transaction_no": PO, "line_number": 1, "section_number": 1, "lot_number": "LOT-9"},
            {"transaction_no": PO, "line_number": 1, "section_number": 2, "lot_number": "LOT-7"},
            {"transaction_no": PO, "line_number": 2, "section_number": 1, "lot_number": "L-3"},
            {"transaction_no": PO, "line_number": 3, "section_number": 1, "lot_number": "LOT-1"},
        ],
        "po_box": [
            _po_box(MI_A1, 1, 1, 1, net="24.500", gross="25.000"),
            _po_box(MI_A2, 1, 2, 1, net="24.000"),
            _po_box(MI_A3, 1, 1, 2, lot=" LOT-9 "),
            _po_box(MI_C, 2, 1, 1, net="10.000", gross="10.200"),
            _po_box(MI_P, 3, 1, 1, net="12.000", gross="12.300"),
        ],
        "all_sku": [
            {"sku_id": 1, "particulars": "ALMOND 25KG", "item_type": "rm", "item_group": "NUTS", "sub_group": "ALMOND",
             "uom": Decimal("25.000")},
            {"sku_id": 2, "particulars": "CASHEW W320", "item_type": "rm", "item_group": "NUTS", "sub_group": "CASHEW",
             "uom": Decimal("10.000")},
            {"sku_id": 3, "particulars": "almond 25kg", "item_type": "sfg", "item_group": "WIP", "sub_group": "WIP",
             "uom": None},
        ],
        "inventory_batch": [{"batch_id": MI_A1, "sku_name": "almond  25kg", "transaction_no": PO, "lot_number": "LOT-9",
                             "warehouse_id": "A185", "status": "AVAILABLE", "entity": entity}],
    }


def _lookup_db(material_in=None, **tables) -> MaterialInDB:
    """Every table the lookups read (empty), the transfer and receipt tables, Material In."""
    return MaterialInDB(**{**{t: [] for t in (*cold.LOOKUP_TABLES, *ro.TRANSFER_TABLES)},
                           **(material_in or _material_in()), **tables})


def _park_db(material_in=None, **tables) -> MaterialInDB:
    """The stock tables park_boxes reads (empty), Material In, then `tables`."""
    return MaterialInDB(**{**{t: [] for t in cold.WAREHOUSE_TABLES}, **(material_in or _material_in()), **tables})


def _request_db(material_in=None) -> MaterialInDB:
    """dl's Approved request (A-185 -> W202: 2 x ALMOND 25KG, 1 x CASHEW W320 of lot L-3) and
    its cold stock, the receipt tables, Material In."""
    return MaterialInDB(**{**dl._db().tables, "interunit_transfer_in_header": [], "interunit_transfer_in_boxes": [],
                           **(material_in or _material_in())})


def _scan_db(material_in=None) -> MaterialInDB:
    """The scan screen's database (test_transfer_scan_save) with Material In."""
    return MaterialInDB(**{**ss._db().tables, **(material_in or _material_in())})


def _material_in_rows(db) -> dict:
    return copy.deepcopy({t: db.tables[t] for t in MATERIAL_IN if t in db.tables})


def _record_only(db, before) -> None:
    """Material In's rows as they were, and no write to them or to a stock table."""
    assert _material_in_rows(db) == before
    assert cold.stock_writes(db) == []


def _payload(**over) -> dict:
    """MI_A1 as the lookups return it."""
    box = {
        "box_id": MI_A1, "transaction_no": PO, "box_number": 1, "article_description": "ALMOND 25KG",
        "item_description": "ALMOND 25KG", "sku_id": None, "item_category": "NUTS", "sub_category": "ALMOND",
        "material_type": "RM", "net_weight": 24.5, "gross_weight": 25.0, "lot_number": "LOT-9", "batch_number": "",
        "uom": "25.000", "quantity_units": None, "packaging_type": None, "quality_grade": None, "count": None,
        "source": "material_in", "company": "cfpl", "inward_site": "A185", "po_number": PO_NO,
        "tally_name": "almond  25kg", "article_basis": "exact",
    }
    box.update(over)
    return box


def _union_calls(db) -> list:
    return [(k, s, a) for k, s, a in db.calls if s.startswith("SELECT * FROM (")]


def _forget_tables() -> None:
    """lookup_service caches which tables exist for the process: forget them before a test
    reads a second database built with other tables."""
    lookup_service._EXISTS.clear()


# ── IMS first ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["manual", "tr", "be"])
def test_the_ims_search_is_sent_as_it_was_whether_or_not_material_in_exists(path):
    with_mi, without = _lookup_db(), cold._db(*cold.LOOKUP_TABLES, *ro.TRANSFER_TABLES)
    for db in (with_mi, without):
        _forget_tables()
        with pytest.raises(HTTPException) as exc:
            cold._lookup(db, path, box_id="NO-SUCH-BOX", box_number=9, tno=PO)
        assert exc.value.status_code == 404
    assert _union_calls(with_mi) == _union_calls(without) and len(_union_calls(with_mi)) == 1
    assert with_mi.unions == without.unions
    # The IMS tables are probed as they were, before anything of Material In.
    ims = [p for p in with_mi.probes if p not in MATERIAL_IN]
    assert ims == without.probes[:len(ims)] and with_mi.probes[:len(ims)] == ims
    if path == "be":
        assert with_mi.po_reads() == [] and not set(with_mi.probes) & set(MATERIAL_IN)
    else:
        assert with_mi.po_reads() == [("box_number" if path == "manual" else "box_id",
                                       (9 if path == "manual" else "NO-SUCH-BOX", PO))]


@pytest.mark.parametrize("path", ["manual", "tr"])
def test_an_ims_row_with_the_same_key_wins_and_material_in_is_not_read(path):
    db = _lookup_db(cfpl_boxes_v2=[cold._v2(box_id=MI_A1, transaction_no=PO, box_number=1)],
                    cfpl_articles_v2=[cold._art(transaction_no=PO)])
    got = cold._lookup(db, path, box_id=MI_A1, box_number=1, tno=PO)
    assert got == {**cold.V2_BOX, "box_id": MI_A1, "transaction_no": PO, "box_number": 1}
    assert db.po_reads() == [] and not set(db.probes) & set(MATERIAL_IN)


def test_a_material_in_qr_is_found_once_the_ims_search_misses():
    db = _lookup_db()
    assert cold._lookup(db, "tr", box_id=MI_A1, tno=PO) == _payload()
    sent = [s for _, s, _ in db.calls if s != "SELECT to_regclass($1)"]
    # The UNION (missing), then the box, its Tally name in the item master, then its transfers.
    assert sent[0].startswith("SELECT * FROM (") and sent[1:3] == [_PO_BOX_SQL["box_id"], _PO_SKU_SQL]
    assert sent[3] == ro.GUARD
    # Material In's tables are probed after IMS's, po_box first, once for the process.
    assert [p for p in db.probes if p in MATERIAL_IN] == ["po_box", "po_header", "po_line", "po_section", "all_sku"]
    assert db.probes.index("po_box") > db.probes.index("cdpl_cold_stocks")
    probes = len(db.probes)
    assert cold._lookup(db, "tr", box_id=MI_C, tno=PO)["item_description"] == "CASHEW W320"
    assert len(db.probes) == probes


def test_a_material_in_box_is_matched_trimmed_and_the_ims_search_gets_the_values_as_sent():
    db = _lookup_db()
    assert cold._lookup(db, "tr", box_id=f" {MI_A1} ", tno=f"{PO}  ") == _payload()
    assert [a for _, _, a in _union_calls(db)] == [(f" {MI_A1} ", f"{PO}  ")]
    assert db.po_reads() == [("box_id", (MI_A1, PO))]


def test_the_be_path_never_reads_material_in():
    db = _lookup_db()
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, "be", box_id=MI_A1, tno=PO)
    assert exc.value.status_code == 404
    assert db.po_reads() == [] and not set(db.probes) & set(MATERIAL_IN)


@pytest.mark.parametrize("path", ["manual", "tr"])
def test_without_the_po_tables_a_miss_is_404_after_one_probe(path):
    db = cold._db(*cold.LOOKUP_TABLES)
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, path, box_id=MI_A1, box_number=1, tno=PO)
    assert exc.value.status_code == 404
    (n,) = [i for i, (_, s, _) in enumerate(db.calls) if s.startswith("SELECT * FROM (")]
    assert [(s, a) for _, s, a in db.calls[n + 1:]] == [("SELECT to_regclass($1)", ("public.po_box",))]
    # Cached: the next miss does not probe again.
    with pytest.raises(HTTPException):
        cold._lookup(db, path, box_id=MI_A2, box_number=2, tno=PO)
    assert db.probes.count("po_box") == 1


def test_without_one_of_the_po_tables_material_in_is_not_read():
    tables = _material_in()
    del tables["po_section"]
    db = _lookup_db(material_in=tables)
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, "tr", box_id=MI_A1, tno=PO)
    assert exc.value.status_code == 404
    assert db.po_reads() == [] and [p for p in db.probes if p in MATERIAL_IN] == [
        "po_box", "po_header", "po_line", "po_section"]


# ── The box payload ──────────────────────────────────────────────────────────
def test_a_material_in_box_has_the_keys_and_types_of_an_ims_box_plus_its_own():
    box = cold._lookup(_lookup_db(), "tr", box_id=MI_A1, tno=PO)
    assert set(box) == set(lookup_service._box_from_row({})) | {
        "source", "company", "inward_site", "po_number", "tally_name", "article_basis"}
    assert all(isinstance(box[k], float) for k in ("net_weight", "gross_weight"))
    assert isinstance(box["box_number"], int) and box["sku_id"] is None and box["batch_number"] == ""


@pytest.mark.parametrize("box_id, number, lot, net, gross", [
    (MI_A1, 1, "LOT-9", 24.5, 25.0),    # no lot of its own: its section's
    (MI_A2, 1, "LOT-7", 24.0, 0.0),     # number 1 again, in the line's other lot section; no gross weight
    (MI_A3, 2, "LOT-9", 0.0, 0.0),      # its own lot, trimmed; no weights
])
def test_the_lot_is_the_boxs_else_its_sections_and_a_missing_weight_is_zero(box_id, number, lot, net, gross):
    got = cold._lookup(_lookup_db(), "tr", box_id=box_id, tno=PO)
    assert (got["box_id"], got["box_number"], got["lot_number"], got["net_weight"], got["gross_weight"]) == (
        box_id, number, lot, net, gross)


def test_a_box_with_no_lot_and_no_section_lot_has_none():
    tables = _material_in()
    tables["po_section"][0]["lot_number"] = "  "
    assert cold._lookup(_lookup_db(material_in=tables), "tr", box_id=MI_A1, tno=PO)["lot_number"] is None
    assert mis._lot(None, None) is None and mis._lot(" B ", "S") == "B" and mis._lot("", " S ") == "S"


@pytest.mark.parametrize("box_id, article, basis, category, sub_category, material_type, uom", [
    (MI_A1, "ALMOND 25KG", "exact", "NUTS", "ALMOND", "RM", "25.000"),    # the item master's row
    (MI_C, "CASHEW W320", "matched", "NUTS", "CASHEW", "RM", "10.0"),     # the PO line's
    (MI_P, "Pista Kernel", "tally", "", "", None, None),
])
def test_each_rule_names_the_box_and_says_where_its_category_comes_from(box_id, article, basis, category,
                                                                       sub_category, material_type, uom):
    got = cold._lookup(_lookup_db(), "tr", box_id=box_id, tno=PO)
    assert (got["article_description"], got["item_description"], got["article_basis"]) == (article, article, basis)
    assert (got["item_category"], got["sub_category"], got["material_type"], got["uom"]) == (
        category, sub_category, material_type, uom)


@pytest.mark.parametrize("entity, warehouse, company, site", [
    ("cfpl", "Warehouse A185", "cfpl", "A185"),
    ("cdpl", "Rishi Cold Storage Pvt Ltd", "cdpl", "Rishi"),
    ("cdpl", "Godown X", "cdpl", "Godown X"),     # a name the site table does not know stays itself
    ("cfpl", None, "cfpl", None),
    ("cfpl", "  ", "cfpl", None),
])
def test_the_payload_names_the_pos_company_and_the_site_it_was_received_into(entity, warehouse, company, site):
    got = cold._lookup(_lookup_db(material_in=_material_in(entity=entity, warehouse=warehouse)), "tr",
                       box_id=MI_A1, tno=PO)
    assert (got["company"], got["inward_site"], got["po_number"], got["source"]) == (company, site, PO_NO,
                                                                                    "material_in")


def test_a_box_never_transferred_is_free_from_any_site():
    # Where it is is its transfer history alone (decided 2026-10-16): no inward-site rule.
    for site in (None, "A185", "W202", "Rishi", "Savla D-39"):
        assert cold._lookup(_lookup_db(), "tr", box_id=MI_A1, tno=PO, from_wh=site)["box_id"] == MI_A1


# ── Refusals ─────────────────────────────────────────────────────────────────
DELETED = datetime(2026, 10, 15, 18, 0)


def _deleted(box_id) -> str:
    return (f"Box {box_id} belongs to PO {PO_NO} ({PO}), which was deleted in Purchase; it can't be sent. "
            "If the goods were entered again on another PO, use that PO's label.")


@pytest.mark.parametrize("path, box_id", [("tr", MI_A1), ("manual", MI_A3)])
def test_a_box_of_a_deleted_po_is_409(path, box_id):
    db = _lookup_db(material_in=_material_in(deleted_at=DELETED))
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, path, box_id=box_id, box_number=2, tno=PO)
    assert (exc.value.status_code, exc.value.detail) == (409, _deleted(box_id))


def test_a_deleted_po_without_a_po_number_is_named_by_its_transaction():
    assert mis._deleted_po({"box_id": "B-1", "transaction_no": "TR-1", "po_number": " "}) == (
        "Box B-1 belongs to PO TR-1, which was deleted in Purchase; it can't be sent. "
        "If the goods were entered again on another PO, use that PO's label.")


def test_a_request_dispatch_with_a_box_of_a_deleted_po_is_409_before_anything_is_written():
    db = _request_db(material_in=_material_in(deleted_at=DELETED))
    assert br._refused(db, dl._payload(boxes=[br._box(MI_A1, tno=PO)]), locks=False) == _deleted(MI_A1)


AMBIGUOUS = (f"Box #1 of {PO} matches more than one Material In box (box numbers restart in every lot section). "
             "Scan its QR label or enter its box ID.")


def test_manual_entry_finds_a_number_one_box_carries_and_refuses_one_several_carry():
    db = _lookup_db()
    assert cold._lookup(db, "manual", box_number=2, tno=PO) == _payload(
        box_id=MI_A3, box_number=2, net_weight=0.0, gross_weight=0.0)
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, "manual", box_number=1, tno=f" {PO} ")
    assert (exc.value.status_code, exc.value.detail) == (409, AMBIGUOUS)
    # Its QR label (the box id) finds each of them.
    assert [cold._lookup(db, "tr", box_id=b, tno=PO)["lot_number"] for b in (MI_A1, MI_A2)] == ["LOT-9", "LOT-7"]
    with pytest.raises(HTTPException) as exc:
        cold._lookup(db, "manual", box_number=7, tno=PO)
    assert exc.value.status_code == 404 and exc.value.detail == f"Box #7 with transaction_no '{PO}' not found"


@pytest.mark.parametrize("what", ["lot", "article"])
def test_a_lot_or_an_article_longer_than_a_transfer_records_is_409(what):
    tables = _material_in()
    if what == "lot":
        tables["po_box"][0]["lot_number"] = "L" * 51
        detail = (f"Box {MI_A1}: its lot '{'L' * 51}' is longer than 50 characters, which a transfer can't record; "
                  "shorten it in Purchase > Material In.")
    else:
        tables["po_line"][0].update(sku_name="A" * 256, particulars=None)
        detail = (f"Box {MI_A1}: its item name '{'A' * 40}...' is longer than 255 characters, which a transfer "
                  "can't record; shorten the item name in Purchase.")
    with pytest.raises(HTTPException) as exc:
        cold._lookup(_lookup_db(material_in=tables), "tr", box_id=MI_A1, tno=PO)
    assert (exc.value.status_code, exc.value.detail) == (409, detail)
    # A request dispatch refuses it the same way, before anything is written.
    _forget_tables()
    assert br._refused(_request_db(material_in=tables), dl._payload(boxes=[br._box(MI_A1, tno=PO)]),
                       locks=False) == detail


def test_a_lot_of_50_and_an_article_of_255_characters_are_taken():
    tables = _material_in()
    tables["po_box"][0]["lot_number"] = "L" * 50
    tables["po_line"][0].update(sku_name="A" * 255, particulars=None)
    got = cold._lookup(_lookup_db(material_in=tables), "tr", box_id=MI_A1, tno=PO)
    assert (got["lot_number"], got["item_description"], got["article_basis"]) == ("L" * 50, "A" * 255, "tally")


# ── The article: resolve_article ─────────────────────────────────────────────
EXACT, MATCHED, TALLY = "exact", "matched", "tally"


def _all(article, basis) -> dict:
    return {mode: (article, basis) for mode in mis.FUZZY_MODES}


@pytest.mark.parametrize("sku_name, particulars, score, exact, expected", [
    # The exact item-master name first, whatever was matched.
    ("almond  25KG", "PISTA", Decimal("0.990"), "ALMOND 25KG", _all("ALMOND 25KG", EXACT)),
    # A matched name with the same numbers (whitespace collapsed).
    ("Cashew W-320 Premium", "CASHEW  W320", Decimal("0.820"), None,
     {"never": ("Cashew W-320 Premium", TALLY), "same_numbers": ("CASHEW W320", MATCHED),
      "always": ("CASHEW W320", MATCHED)}),
    # Neighbouring grades the matcher confuses: only "always" takes them.
    ("Cashew 320", "Cashew W240", Decimal("0.800"), None,
     {"never": ("Cashew 320", TALLY), "same_numbers": ("Cashew 320", TALLY), "always": ("Cashew W240", MATCHED)}),
    ("Pumpkin Seed 12%", "Pumpkin Seed 10%", Decimal("0.812"), None,
     {"never": ("Pumpkin Seed 12%", TALLY), "same_numbers": ("Pumpkin Seed 12%", TALLY),
      "always": ("Pumpkin Seed 10%", MATCHED)}),
    # The floor is 0.70: at it the matched name, below it the Tally name.
    ("Almonds 25-27 count", "American Almonds Running 25-27 count", Decimal("0.700"), None,
     {"never": ("Almonds 25-27 count", TALLY), "same_numbers": ("American Almonds Running 25-27 count", MATCHED),
      "always": ("American Almonds Running 25-27 count", MATCHED)}),
    ("Almonds 25-27 count", "American Almonds Running 25-27 count", Decimal("0.699"), None,
     _all("Almonds 25-27 count", TALLY)),
    # No matched name (NULL or blank), or no score: the Tally name, whitespace collapsed.
    ("Mystery Nut", None, None, None, _all("Mystery Nut", TALLY)),
    (" Mystery   Nut ", "  ", Decimal("0.950"), None, _all("Mystery Nut", TALLY)),
    ("Mystery Nut", "Sayer Dates", None, None, _all("Mystery Nut", TALLY)),
    (None, None, None, None, _all("", TALLY)),
], ids=["exact", "same-numbers", "cashew-320-w240", "pumpkin-12-10", "at-the-floor", "below-the-floor",
        "null-particulars", "blank-particulars", "no-score", "no-names"])
@pytest.mark.parametrize("mode", mis.FUZZY_MODES)
def test_resolve_article_under_each_setting(monkeypatch, mode, sku_name, particulars, score, exact, expected):
    monkeypatch.setattr(mis, "FUZZY_NAMES", mode)
    assert mis.resolve_article(sku_name, particulars, score, exact) == expected[mode]


def test_the_matched_name_is_used_always_as_decided():
    assert (mis.FUZZY_NAMES, mis.MATCH_FLOOR) == ("always", 0.70) and mis.FUZZY_NAMES in mis.FUZZY_MODES
    assert (mis.LOT_MAX, mis.ARTICLE_MAX, mis.SOURCE_TABLE) == (50, 255, "po_box")


def test_the_exact_name_ignores_case_and_spaces_and_is_never_a_semi_finished_item():
    db = _lookup_db()
    box = cold._lookup(db, "tr", box_id=MI_A1, tno=PO)
    assert (box["item_description"], box["article_basis"], box["tally_name"]) == ("ALMOND 25KG", "exact",
                                                                                 "almond  25kg")
    assert [a for _, s, a in db.calls if s == _PO_SKU_SQL] == [("ALMOND 25KG",)]
    # Only the semi-finished twin: no exact name; the matched one scored below the floor.
    tables = _material_in()
    tables["all_sku"] = [a for a in tables["all_sku"] if a["item_type"] == "sfg"]
    box = cold._lookup(_lookup_db(material_in=tables), "tr", box_id=MI_A1, tno=PO)
    assert (box["item_description"], box["article_basis"], box["item_category"]) == ("almond 25kg", "tally",
                                                                                     "DRY FRUIT")


def test_without_the_item_master_the_line_names_the_box():
    tables = _material_in()
    del tables["all_sku"]
    db = _lookup_db(material_in=tables)
    assert cold._lookup(db, "tr", box_id=MI_C, tno=PO)["article_basis"] == "matched"
    assert cold._lookup(db, "tr", box_id=MI_A1, tno=PO)["article_basis"] == "tally"
    assert not [s for _, s, _ in db.calls if s == _PO_SKU_SQL] and db.probes.count("all_sku") == 1


# ── R1: a request dispatch judges Material In boxes by their own records ────
def test_a_request_dispatch_judges_and_stores_material_in_boxes_by_their_material_in_records():
    db = _request_db()
    before = _material_in_rows(db)
    # Sent as something else: judged, and stored, as Material In has them.
    dl._create(db, dl._payload(boxes=[br._box(MI_A1, "PISTA", "LOT-1", tno=PO, n=1), br._box(MI_A2, tno=PO, n=2),
                                      br._box(MI_C, "CASHEW W320", "L-9", tno=PO, n=3)]))
    assert [(b["box_id"], b["article"], b["lot_number"]) for b in db.tables["interunit_transfer_boxes"]] == [
        (MI_A1, "ALMOND 25KG", "LOT-9"), (MI_A2, "ALMOND 25KG", "LOT-7"), (MI_C, "CASHEW W320", "L-3")]
    h = dl._header(db)
    assert (h["status"], h["unallocated_boxes"]) == ("Dispatch", 0) and dl._phantoms(db) == []
    assert {k: (p["source_table"], p["item_description"], p["lot_no"])
            for k, p in ro._parked(db, h["id"]).items()} == {
        MI_A1: ("po_box", "ALMOND 25KG", "LOT-9"), MI_A2: ("po_box", "ALMOND 25KG", "LOT-7"),
        MI_C: ("po_box", "CASHEW W320", "L-3")}
    _record_only(db, before)


@pytest.mark.parametrize("fuzzy, box, detail", [
    ("always", br._box(MI_P, "PISTA", "LOT-1", tno=PO, n=3),
     f"Box {MI_P} is Pista Kernel, which is not on request {REQ}."),
    # Without the matched name the cashew box carries its Tally name, which no request names.
    ("never", br._box(MI_C, "CASHEW W320", "L-3", tno=PO, n=3),
     f"Box {MI_C} is Cashew W-320 Premium, which is not on request {REQ}."),
], ids=["no-item-master-name", "matched-name-not-used"])
def test_a_request_dispatch_refuses_a_material_in_box_the_request_does_not_ask_for(monkeypatch, fuzzy, box, detail):
    monkeypatch.setattr(mis, "FUZZY_NAMES", fuzzy)
    db = _request_db()
    assert br._refused(db, dl._payload(boxes=[br._box(MI_A1, tno=PO), box]), locks=False) == detail


def test_a_material_in_box_under_another_transaction_is_not_found():
    db = _request_db()
    assert br._refused(db, dl._payload(boxes=[br._box(MI_A1, tno="TR-20261016093001")]), locks=False) == (
        f"Box {MI_A1} with transaction TR-20261016093001 was not found.")


# ── park_boxes ───────────────────────────────────────────────────────────────
def _mi_box(**over) -> dict:
    """MI_A1 as create_service hands it to park_boxes (the client's weights)."""
    return cold._box(**{"box_id": MI_A1, "transaction_no": PO, "lot_number": "LOT-9", **over})


def _mi_parked(**over) -> dict:
    """MI_A1's in-transit row parked from A185 to W202: a warehouse copy of its po_box row."""
    return cold._parked(**{
        "box_id": MI_A1, "transaction_no": PO, "from_company": "cfpl", "to_company": "cfpl",
        "source_table": "po_box", "source_row_id": None, "destination_table": "cfpl_boxes_v2",
        "item_description": "ALMOND 25KG", "lot_no": "LOT-9", "weight_kg": Decimal("24.5"), "cold_storage_data": None,
        "gross_weight": Decimal("25.0"), "net_weight": Decimal("24.5"), "article": "ALMOND 25KG", **over})


@pytest.mark.parametrize("cold_fallback", [True, False], ids=["dispatch", "backfill"])
def test_a_material_in_box_parks_as_a_warehouse_copy_of_its_po_box_row(cold_fallback):
    db = _park_db()
    before = _material_in_rows(db)
    assert cold._park(db, [_mi_box()], cold_fallback=cold_fallback) == {**cold.NO_STATS, "parked_from_warehouse": 1}
    assert cold._pending(db) == _mi_parked()
    _record_only(db, before)


def test_a_material_in_box_without_weights_parks_with_the_boxs_net_weight():
    db = _park_db()
    cold._park(db, [_mi_box(box_id=MI_A3, net_weight="23.75")])
    p = cold._pending(db)
    assert (p["weight_kg"], p["net_weight"], p["gross_weight"]) == (Decimal("23.75"), Decimal("23.75"), Decimal("0.0"))


@pytest.mark.parametrize("entity, from_site, to_site, companies, storage, destination", [
    ("cfpl", "A185", "W202", ("cfpl", "cfpl"), ("warehouse", "warehouse"), "cfpl_boxes_v2"),
    ("cdpl", "A185", "W202", ("cdpl", "cdpl"), ("warehouse", "warehouse"), "cdpl_boxes_v2"),
    # At a cold store of the other company: the PO's company all the same.
    ("cfpl", "Rishi", "W202", ("cfpl", "cfpl"), ("cold", "warehouse"), "cfpl_boxes_v2"),
    ("cdpl", "Savla D-39", "A185", ("cdpl", "cdpl"), ("cold", "warehouse"), "cdpl_boxes_v2"),
    # A cold store To names its own.
    ("cfpl", "A185", "Rishi", ("cfpl", "cdpl"), ("warehouse", "cold"), "cdpl_cold_stocks"),
    ("cdpl", "Rishi", "Savla D-39", ("cdpl", "cfpl"), ("cold", "cold"), "cfpl_cold_stocks"),
])
def test_a_material_in_copy_is_its_pos_company_from_any_site(entity, from_site, to_site, companies, storage,
                                                             destination):
    db = _park_db(material_in=_material_in(entity=entity))
    assert cold._park(db, [_mi_box()], from_site=from_site, to_site=to_site) == {
        **cold.NO_STATS, "parked_from_warehouse": 1}
    p = cold._pending(db)
    assert (p["from_company"], p["to_company"]) == companies
    assert (p["from_storage_type"], p["to_storage_type"], p["destination_table"]) == (*storage, destination)
    assert (p["source_table"], p["source_row_id"], p["cold_storage_data"]) == ("po_box", None, None)


@pytest.mark.parametrize("cold_fallback", [True, False])
def test_at_a_cold_from_site_a_material_in_box_is_never_copied_from_an_ims_cold_row_sharing_its_box_id(cold_fallback):
    # IMS and ERP mint box ids alike: cold row 34732254-5 / TR-2026-0042 is another box.
    tables = _material_in()
    tables["po_box"].append(_po_box(cold.BOX_ID, 1, 1, 3, net="24.000", gross="24.400"))
    db = _park_db(material_in=tables, cfpl_cold_stocks=[cold._cold()])
    stats = cold._park(db, [cold._box(transaction_no=PO, lot_number="LOT-9")], from_site="Savla D-39",
                       cold_fallback=cold_fallback)
    assert stats == {**cold.NO_STATS, "parked_from_warehouse": 1}
    p = cold._pending(db)
    assert (p["source_table"], p["source_row_id"], p["cold_storage_data"], p["item_description"], p["weight_kg"]) == (
        "po_box", None, None, "ALMOND 25KG", Decimal("24.0"))
    assert not [s for s in db.statements("SELECT *") if s.endswith("WHERE box_id = $1 LIMIT 1")]
    assert db.tables["cfpl_cold_stocks"] == [cold._cold()] and cold.stock_writes(db) == []


IMS_PARKS = [
    ("A185", True, {"cdpl_cold_stocks": [cold._cold()]}, cold._box()),               # cold-only box at a warehouse
    ("A185", False, {"cdpl_cold_stocks": [cold._cold()]}, cold._box()),              # backfill: no source
    ("Rishi", True, {"cfpl_boxes_v2": [cold._v2()], "cfpl_cold_stocks": [cold._cold()]}, cold._box()),
    ("Savla D-39", True, {"cfpl_cold_stocks": [cold._cold(transaction_no="TR-2026-0001")]}, cold._box()),
    ("Rishi", False, {"cfpl_boxes_v2": [cold._v2()]}, cold._box()),                 # cold From, then boxes_v2
    ("A185", True, {"cfpl_bulk_entry_boxes": [cold._bulk()]},
     cold._box(box_id="88120417-3", transaction_no="BE-0007")),
    ("A185", True, {}, cold._box()),                                                  # in no stock table
    ("Rishi", True, {}, cold._box()),
]


@pytest.mark.parametrize("from_site, cold_fallback, stock, box", IMS_PARKS,
                         ids=["warehouse-cold-only", "backfill", "cold-exact", "cold-box-id-alone", "cold-then-v2",
                              "bulk", "nowhere", "cold-nowhere"])
def test_an_ims_box_parks_exactly_as_it_did_with_material_in_there(from_site, cold_fallback, stock, box):
    # Material In has a box with the same box id, under its own PO.
    tables = _material_in()
    tables["po_box"].append(_po_box(box["box_id"], 1, 1, 3, net="1.000", gross="1.000"))
    with_mi = _park_db(material_in=tables, **copy.deepcopy(stock))
    without = cold._db(*cold.WAREHOUSE_TABLES, **copy.deepcopy(stock))
    got = cold._park(with_mi, [box], from_site=from_site, cold_fallback=cold_fallback)
    _forget_tables()
    assert got == cold._park(without, [box], from_site=from_site, cold_fallback=cold_fallback)
    assert cold._pending(with_mi) == cold._pending(without)
    assert cold.stock_writes(with_mi) == []


def _clash() -> dict:
    """Material In with a box of IMS's box's very key (cold.BOX_ID under TNO, on a PO of that
    transaction): an exact (box_id, transaction_no) collision, possible with legacy TR- transaction
    numbers (the reconciliation's MI-4 lists them)."""
    tables = _material_in()
    for table in ("po_header", "po_line", "po_section"):
        tables[table].append({**tables[table][0], "transaction_no": TNO})
    tables["po_box"].append(_po_box(cold.BOX_ID, 1, 1, 1, net="1.000", gross="1.000", tno=TNO))
    return tables


@pytest.mark.parametrize("from_site, stock, source", [
    ("Rishi", {"cfpl_cold_stocks": [cold._cold()]}, "cfpl_cold_stocks"),
    ("Rishi", {"cfpl_boxes_v2": [cold._v2()]}, "cfpl_boxes_v2"),
    ("A185", {"cfpl_boxes_v2": [cold._v2()]}, "cfpl_boxes_v2"),
    ("A185", {"cdpl_cold_stocks": [cold._cold()]}, "cdpl_cold_stocks"),
], ids=["cold-from-site", "cold-from-site-boxes-v2", "warehouse-boxes-v2", "warehouse-cold-only"])
def test_an_ims_row_with_a_material_in_boxs_very_key_is_the_one_copied_as_the_lookup_finds_it(from_site, stock,
                                                                                                source):
    # The lookups return IMS's row (_clash), so a dispatch copies that one.
    db = _park_db(material_in=_clash(), **copy.deepcopy(stock))
    cold._park(db, [cold._box()], from_site=from_site)
    assert cold._pending(db)["source_table"] == source


def _park_box(db, *, from_site, cold_fallback, strict_cold) -> dict:
    """cold._park of cold._box(), with `strict_cold` (a receipt undone) as well."""
    async def go():
        async with db.transaction():
            return await reversal_service.park_boxes(
                db, transfer_out_id=cold.TOUT, challan_no=cold.CHALLAN, from_site=from_site, to_site="W202",
                boxes=[cold._box()], dispatched_at=cold.DISPATCHED, dispatched_by=USER,
                cold_fallback=cold_fallback, strict_cold=strict_cold)
    return asyncio.run(go())


@pytest.mark.parametrize("from_site, cold_fallback, strict_cold, stock", [
    ("A185", True, False, {"cdpl_cold_stocks": [cold._cold()]}),
    ("A185", False, False, {"cdpl_cold_stocks": [cold._cold()]}),
    ("A185", True, False, {"cfpl_boxes_v2": [cold._v2()]}),
    ("A185", True, False, {"cfpl_bulk_entry_boxes": [cold._bulk(box_id=cold.BOX_ID, transaction_no=TNO)]}),
    ("Rishi", True, False, {"cfpl_cold_stocks": [cold._cold()]}),
    ("Rishi", True, False, {"cfpl_boxes_v2": [cold._v2()]}),
    ("Rishi", False, False, {"cdpl_bulk_entry_boxes": [cold._bulk(box_id=cold.BOX_ID, transaction_no=TNO)]}),
    ("Rishi", True, True, {"cfpl_boxes_v2": [cold._v2()]}),
    ("Rishi", True, False, {"cfpl_boxes_v2": [cold._v2()],
                            "cfpl_cold_stocks": [cold._cold(transaction_no="TR-2026-0001")]}),
], ids=["warehouse-cold-only", "backfill-cold-only-no-source", "warehouse-boxes-v2", "warehouse-bulk",
        "cold-exact", "cold-then-boxes-v2", "cold-then-bulk-backfill", "receipt-undone-boxes-v2",
        "cold-box-id-alone-before-boxes-v2"])
def test_an_ims_box_parks_exactly_as_it_did_when_a_material_in_box_has_its_very_key(from_site, cold_fallback,
                                                                                    strict_cold, stock):
    # The lookups find IMS's row first (_clash), so the box is IMS's: whichever IMS table has
    # it, whatever the From site, it parks as it did without Material In, never from po_box.
    with_mi = _park_db(material_in=_clash(), **copy.deepcopy(stock))
    without = cold._db(*cold.WAREHOUSE_TABLES, **copy.deepcopy(stock))
    got = _park_box(with_mi, from_site=from_site, cold_fallback=cold_fallback, strict_cold=strict_cold)
    _forget_tables()
    assert got == _park_box(without, from_site=from_site, cold_fallback=cold_fallback, strict_cold=strict_cold)
    assert cold._pending(with_mi) == cold._pending(without)
    assert cold._pending(with_mi)["source_table"] != "po_box" and cold.stock_writes(with_mi) == []


@pytest.mark.parametrize("deleted", [False, True], ids=["po-live", "po-deleted-since"])
def test_a_receipt_undone_reparks_a_material_in_box_from_po_box_at_a_cold_from_site(deleted):
    # A cold row that merely shares the box's id (another inward) is never copied (strict_cold).
    namesake = cold._cold(id=9999, box_id=MI_A1, transaction_no="TR-OTHER-INWARD", item_description="CASHEW W240")
    db = _park_db(**{t: [] for t in ro.TRANSFER_TABLES}, cfpl_cold_stocks=[namesake])
    tid = dl._create(db, dl._payload(request_id=None, from_wh="Rishi", to_wh="W202",
                                     boxes=[br._box(MI_A1, "ALMOND 25KG", "LOT-9", tno=PO)]))["id"]
    dispatched = ro._parked(db, tid)
    assert (dispatched[MI_A1]["source_table"], dispatched[MI_A1]["from_storage_type"]) == ("po_box", "cold")
    ro._receipt(db, tid)
    ro._finalize(db)
    assert ro._parked(db, tid) == {}
    if deleted:
        db.tables["po_header"][0]["deleted_at"] = DELETED
    before = _material_in_rows(db)

    asyncio.run(receive_service.reopen_transfer_in(db, ro.TI, USER))
    assert ro._parked(db, tid) == dispatched
    assert not [s for s in db.statements("SELECT *") if s.endswith("WHERE box_id = $1 LIMIT 1")]
    assert db.tables["cfpl_cold_stocks"] == [namesake]
    _record_only(db, before)


def test_the_backfill_copies_a_material_in_box_from_po_box():
    db = _park_db(**{t: [] for t in ro.TRANSFER_TABLES})
    db.tables["interunit_transfers_header"].append(
        {"id": 5, "challan_no": "TRANS-OLD", "from_site": "A185", "to_site": "W202", "status": "Dispatch",
         "created_by": USER, "created_ts": datetime(2026, 10, 16, 10, 0)})
    db.tables["interunit_transfer_boxes"].append(
        {"id": 6, "header_id": 5, "box_id": MI_C, "article": "CASHEW W320", "lot_number": "L-3", "batch_number": "",
         "transaction_no": PO, "net_weight": Decimal("10"), "gross_weight": Decimal("10.2")})
    before = _material_in_rows(db)
    s = asyncio.run(reversal_service.backfill_pending_from_existing_transfers(db))
    assert s["boxes_parked_from_warehouse"] == 1 and s["boxes_parked_without_source"] == 0
    p = ro._parked(db, 5)[MI_C]
    assert (p["source_table"], p["source_row_id"], p["from_company"], p["item_description"], p["weight_kg"]) == (
        "po_box", None, "cfpl", "CASHEW W320", Decimal("10.0"))
    _record_only(db, before)


# ── An edit and a weight edit keep the dispatch's copy ──────────────────────
def test_an_edit_keeps_a_material_in_boxs_copy_and_reads_po_box_only_for_a_box_it_adds():
    db = _request_db()
    kept = [br._box(MI_A1, tno=PO, n=1), br._box(MI_C, "CASHEW W320", "L-3", tno=PO, n=2)]
    dl._create(db, dl._payload(boxes=kept))
    tid = dl._header(db)["id"]
    before = ro._parked(db, tid)
    # Stores has edited the box in Material In since: the transfer keeps its own copy.
    (row,) = [b for b in db.tables["po_box"] if b["box_id"] == MI_A1]
    row.update(net_weight=Decimal("20.000"), gross_weight=Decimal("20.500"), lot_number="LOT-NEW")
    material_in = _material_in_rows(db)
    n = len(db.calls)

    dl._update(db, dl._payload(from_wh="A185", to_wh="W202", boxes=[*kept, br._box(MI_A2, tno=PO, n=3)]))
    after = ro._parked(db, tid)
    assert after[MI_A1] == before[MI_A1] and after[MI_C] == before[MI_C]
    assert (after[MI_A2]["source_table"], after[MI_A2]["lot_no"]) == ("po_box", "LOT-7")
    assert {b["box_id"]: b["lot_number"] for b in db.tables["interunit_transfer_boxes"]} == {
        MI_A1: "LOT-9", MI_C: "L-3", MI_A2: "LOT-7"}
    # Only the added box was read from Material In: judged (R1), then copied.
    assert db.po_reads(n) == [("box_id", (MI_A2, PO)), ("box_id", (MI_A2, PO))]
    _record_only(db, material_in)


@pytest.mark.parametrize("box_id, po_net, copy_net, net", [
    (MI_A1, "24.500", "24.5", Decimal("24.5")),          # the copy holds its po_box row's weight: kept
    (MI_A1, None, "24.5", 23.0),                         # the row has none: the copy's was the box's own
    (MI_A1, "0.000", "0.0", 23.0),                       # nor has a row of 0 (the box was scanned at 0)
    (MI_A1, "24.000", "24.5", 23.0),                     # another: weighed or corrected in Material In since
    ("71000009-9", "24.500", "24.5", Decimal("24.5")),   # gone from Material In: kept
], ids=["its-own-weight", "no-weight-of-its-own", "a-weight-of-0", "another-weight-since", "gone-from-material-in"])
def test_reweighed_tells_whose_weight_a_material_in_copy_holds_by_its_po_box_row(box_id, po_net, copy_net, net):
    tables = _material_in()
    (row,) = [b for b in tables["po_box"] if b["box_id"] == MI_A1]
    row["net_weight"] = None if po_net is None else Decimal(po_net)
    own = {"box_id": box_id, "transaction_no": PO, "source_table": "po_box", "source_row_id": None,
           "net_weight": Decimal(copy_net), "cold_storage_data": None}
    db = _lookup_db(material_in=tables)
    assert asyncio.run(scan_service._reweighed(db, own, 23.0)) == {**own, "net_weight": net}
    assert db.po_reads() == [("box_id", (box_id, PO))]


def test_a_weight_edit_after_material_in_weighed_the_box_gives_its_copy_the_new_weight():
    # Material In stays editable after a dispatch. MI_A3 went without a weight of its own, so its
    # copy holds the box's (24.75); Stores weighs it in Material In since (23.000). The copy's
    # weight was still the box's own, so it takes the new one, as the box row does.
    db = _scan_db()
    a3 = br._box(MI_A3, tno=PO, n=1)
    ss._scan(db, a3)
    assert ss._parked(db)[MI_A3]["net_weight"] == Decimal("24.75")
    (row,) = [b for b in db.tables["po_box"] if b["box_id"] == MI_A3]
    row["net_weight"] = Decimal("23.000")
    before = _material_in_rows(db)

    ss._weights(db, a3, 21.5, 22.0)
    (box,) = [b for b in db.tables["interunit_transfer_boxes"] if b["box_id"] == MI_A3]
    p = ss._parked(db)[MI_A3]
    assert (box["net_weight"], p["net_weight"], p["weight_kg"]) == (Decimal("21.5"), Decimal("21.5"), Decimal("21.5"))
    assert (p["source_table"], p["gross_weight"]) == ("po_box", Decimal("0.0"))
    _record_only(db, before)


def _created(boxes, material_in=None) -> MaterialInDB:
    """A fresh scan database whose request create_transfer dispatched at once with `boxes`,
    with the first scan's header (test_transfer_scan_save._created, with Material In)."""
    db = _scan_db(material_in)
    payload = schemas.TransferCreate(
        header=schemas.TransferHeaderCreate(challan_no=ss.CHALLAN, stock_trf_date="15-10-2026",
                                            from_warehouse=ss.WAREHOUSE[0], to_warehouse=ss.WAREHOUSE[1],
                                            **ss.TRANSPORT),
        lines=[dl._line("WHATEVER THE CLIENT SENDS")], boxes=list(boxes), request_id=dl.RID)
    asyncio.run(create_service.create_transfer(db, payload, USER))
    return db


def test_scans_removals_and_weight_edits_of_material_in_boxes_leave_what_create_transfer_writes():
    db = _scan_db()
    before = _material_in_rows(db)
    a2, c, a3 = (br._box(MI_A2, tno=PO, n=1), br._box(MI_C, "CASHEW W320", "L-3", tno=PO, n=2),
                 br._box(MI_A3, tno=PO, n=3))
    scanned = [a2, c, a3]
    for n, box in enumerate(scanned, 1):
        ss._scan(db, box)
        assert ss._rows(db) == ss._rows(_created(scanned[:n])), f"after scan {n}"
    ss._remove(db, c)
    assert ss._rows(db) == ss._rows(_created([a2, a3]))
    n = len(db.calls)
    ss._weights(db, a3, 23.9, 24.6)     # no weight of its own in Material In: its copy takes the new one
    ss._weights(db, a2, 23.0, 0)        # its own net weight (24.000) stays the copy's
    assert ss._rows(db) == ss._rows(_created([ss._with_weights(a2, "23.0", "0"), ss._with_weights(a3, "23.9", "24.6")]))
    p = ss._parked(db)
    assert (p[MI_A2]["net_weight"], p[MI_A3]["net_weight"]) == (Decimal("24.0"), Decimal("23.9"))
    assert db.po_reads(n) == [("box_id", (MI_A3, PO)), ("box_id", (MI_A2, PO))]
    _record_only(db, before)


# ── One box per box id on a transfer ────────────────────────────────────────
SHARED = dl.ALMOND_BOXES[0]       # 34732254-1: an IMS cold box of dl's, and a Material In box's id too


def _sharing() -> dict:
    tables = _material_in()
    tables["po_box"].append(_po_box(SHARED, 1, 1, 3, net="24.250", gross="24.700"))
    return tables


def _on_this_transfer(tno, held) -> str:
    return (f"Box {SHARED} of {tno} has the same box id as box {SHARED} of {held} on this transfer; a transfer can "
            "carry only one box with a given box id, so send one of them on another transfer.")


def _already_on(tno, held) -> str:
    return (f"Box {SHARED} of {tno} has the same box id as box {SHARED} of {held}, which is already on transfer "
            f"{ss.CHALLAN}; a transfer can carry only one box with a given box id, so send this one on another "
            "transfer.")


@pytest.mark.parametrize("request_id", [dl.RID, None], ids=["from-a-request", "without-a-request"])
def test_a_dispatch_with_two_boxes_of_one_box_id_is_409_naming_both_transactions(request_id):
    db = _request_db(material_in=_sharing())
    boxes = [br.A1, br._box(SHARED, tno=f" {PO} ", n=2), br.CASHEW]
    with pytest.raises(HTTPException) as exc:
        dl._create(db, dl._payload(boxes=boxes, request_id=request_id, from_wh="A185", to_wh="W202"))
    assert (exc.value.status_code, exc.value.detail) == (409, _on_this_transfer(PO, TNO))
    # No box was stored (the transaction, which this fake does not roll back, undoes the rest).
    assert db.tables["interunit_transfer_boxes"] == [] and db.tables["pending_transfer_stock"] == []


def test_an_edit_that_adds_a_box_of_a_box_id_the_transfer_has_is_409():
    db = _request_db(material_in=_sharing())
    dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", boxes=[br.A1]))
    with pytest.raises(HTTPException) as exc:
        dl._update(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202",
                                   boxes=[br._box(SHARED, tno=PO), br.A1]))
    assert (exc.value.status_code, exc.value.detail) == (409, _on_this_transfer(TNO, PO))


def test_the_same_box_twice_is_still_the_400_it_was():
    db = _request_db()
    with pytest.raises(HTTPException) as exc:
        dl._create(db, dl._payload(request_id=None, from_wh="A185", to_wh="W202", boxes=[br.A1, br.A1]))
    assert exc.value.status_code == 400 and exc.value.detail.startswith(f"Duplicate box_id '{SHARED}'")


def test_a_later_scan_of_a_box_whose_box_id_the_transfer_has_is_409_and_writes_nothing():
    db = _scan_db(_sharing())
    ss._scan(db, ss.A1)
    assert ss._refused(db, lambda: ss._scan(db, br._box(f" {SHARED}", tno=PO, n=2))) == _already_on(PO, TNO)
    # The other way round.
    db = _scan_db(_sharing())
    ss._scan(db, br._box(SHARED, tno=PO, n=1))
    assert ss._refused(db, lambda: ss._scan(db, ss.A1)) == _already_on(TNO, PO)


# ── Record only ──────────────────────────────────────────────────────────────
def test_the_record_only_pin_covers_material_ins_tables():
    db = cold.FakeDB()
    for s in ("INSERT INTO po_box (box_id) VALUES ($1)", "UPDATE po_header SET warehouse = $1",
              "DELETE FROM po_section WHERE transaction_no = $1", "UPDATE inventory_batch SET status = $1",
              "INSERT INTO po_line (sku_name) VALUES ($1)"):
        db.calls.append(("execute", s, ()))
    assert len(cold.stock_writes(db)) == 5


def test_a_material_in_box_through_dispatch_receipt_undo_edit_and_cancel_writes_none_of_material_in():
    db = _request_db()
    before = _material_in_rows(db)
    dl._create(db, dl._payload(boxes=[br._box(MI_A1, tno=PO, n=1), br._box(MI_C, "CASHEW W320", "L-3", tno=PO, n=2)]))
    tid = dl._header(db)["id"]
    ro._receipt(db, tid)
    ro._finalize(db)
    asyncio.run(receive_service.reopen_transfer_in(db, ro.TI, USER))
    asyncio.run(delete_service.delete_transfer_in(db, ro.TI, USER))
    dl._update(db, dl._payload(boxes=[br._box(MI_A1, tno=PO, n=1)]))
    asyncio.run(delete_service.delete_transfer(db, tid))
    assert db.tables["interunit_transfers_header"] == [] and db.tables["pending_transfer_stock"] == []
    _record_only(db, before)
    # Free again: it can be scanned anew.
    assert cold._lookup(db, "tr", box_id=MI_A1, tno=PO)["box_id"] == MI_A1
