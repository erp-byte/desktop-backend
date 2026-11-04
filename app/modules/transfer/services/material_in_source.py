"""ERP Material In boxes (po_box) on Scan & send: the last place the transfer module looks
for a box, after IMS's stock tables. Read only.

Material In (the purchase module) keeps one po_box row per carton weighed in on a PO: its
box_id (the "bi" of its QR label), its PO's transaction_no (the "tx"), its box_number
(which restarts in every lot section of a PO line, so it names a box only together with
its section), its weights and its lot (else its section's lot). The PO header gives the
company (entity), the warehouse it was received into and whether the PO was deleted; the
PO line gives the item names (resolve_article).

  find_box   the box as the lookups return it (box_payload), for lookup_service._find
             once IMS's UNION has missed: the "TR-" QR, manual entry and stock_records
             (create_service judges and records a request dispatch's boxes by it), never
             the "BE-" path. 409 for a box that can't be sent as its record stands: its PO
             was deleted, its box number is carried by more than one box of the PO, or
             its lot or article is longer than a transfer records.
  snapshot   what reversal_service.park_boxes copies into a box's in-transit row
             (source_table SOURCE_TABLE; po_box has no integer id, so no source_row_id),
             and what scan_service._reweighed reads to tell whose weight that copy holds.

Where a box is is decided by its transfer history alone (lookup_service.unavailable_boxes),
as for an IMS box: a Material In box that was never transferred may be sent from any site
(decided 2026-10-16).

READ ONLY: nothing here writes po_header, po_line, po_section, po_box, inventory_batch or
any other table. Without the four po tables every function finds nothing and sends no
statement but the existence probes (to_regclass, cached for the process by
lookup_service._exists; po_box first).
"""
from __future__ import annotations

import re

from fastapi import HTTPException

from app.core import sites

#: pending_transfer_stock.source_table of a box copied from Material In.
SOURCE_TABLE = "po_box"
#: The lowest po_line.match_score (0 to 1) at which the PO line's matched item-master name
#: may name its boxes (resolve_article).
MATCH_FLOOR = 0.70
#: When that matched name is used: "never"; "same_numbers", only when it carries the same
#: numbers as the Tally name; or "always". The user chose "always" on 2026-10-16, knowing
#: the matcher can confuse neighbouring grades ('Cashew 320' -> 'Cashew W240').
FUZZY_NAMES = "always"
FUZZY_MODES = ("never", "same_numbers", "always")
#: The widths of the in-transit columns a box's lot and article are copied into
#: (pending_transfer_stock.lot_no varchar(50), item_description varchar(255)).
LOT_MAX = 50
ARTICLE_MAX = 255

_TABLES = ("po_box", "po_header", "po_line", "po_section")      # po_box first
_NUMBERS = re.compile(r"[0-9]+(?:\.[0-9]+)?")

# A Material In box with its PO header (inner: a box always has one), line and lot section
# (left), by box_id or by box_number ({match}, from _MATCH) and transaction_no. LIMIT 2: a
# box_id is po_box's key, but a box number repeats in every lot section of a PO line.
_MATCH = {"box_id": "b.box_id = $1", "box_number": "b.box_number = $1"}
_BOX_SQL = """
    SELECT b.box_id, b.transaction_no, b.line_number, b.section_number, b.box_number,
           b.net_weight, b.gross_weight, b.lot_number AS box_lot, s.lot_number AS section_lot,
           h.entity, h.warehouse, h.po_number, h.deleted_at,
           l.sku_name, l.particulars, l.match_score, l.item_category, l.sub_category, l.item_type, l.uom
      FROM po_box b
      JOIN po_header h ON h.transaction_no = b.transaction_no
      LEFT JOIN po_line l ON l.transaction_no = b.transaction_no AND l.line_number = b.line_number
      LEFT JOIN po_section s ON s.transaction_no = b.transaction_no AND s.line_number = b.line_number
                            AND s.section_number = b.section_number
     WHERE {match} AND b.transaction_no = $2
     ORDER BY b.line_number, b.section_number, b.box_id
     LIMIT 2
"""

# The item-master (all_sku) row whose name is $1 with case and spaces ignored ($1 is the
# Tally name as _article_key compares names: whitespace collapsed, upper case), never a
# semi-finished one: rm / pm / fg first, then the oldest. uom is all_sku's number, as text.
_EXACT_SKU_SQL = r"""
    SELECT a.particulars, a.item_type, a.item_group, a.sub_group, a.uom::text AS uom
      FROM all_sku a
     WHERE upper(btrim(regexp_replace(a.particulars, '\s+', ' ', 'g'))) = $1
       AND lower(coalesce(a.item_type, '')) <> 'sfg'
     ORDER BY (lower(coalesce(a.item_type, '')) IN ('rm', 'pm', 'fg')) DESC, a.sku_id
     LIMIT 1
"""


async def _exists(conn, table: str) -> bool:
    """lookup_service._exists, with its process cache (which the tests reset). Imported
    here, not at the top: lookup_service imports this module."""
    from app.modules.transfer.services import lookup_service
    return await lookup_service._exists(conn, table)


async def available(conn) -> bool:
    """True when po_box, po_header, po_line and po_section all exist (po_box probed first,
    each once for the process). Without them no other statement is sent."""
    for table in _TABLES:
        if not await _exists(conn, table):
            return False
    return True


def _collapse(name) -> str:
    """A name with its whitespace collapsed; '' for none."""
    return " ".join((name or "").split())


def _score(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _numbers(name) -> list[str]:
    return sorted(_NUMBERS.findall(name or ""))


def _fuzzy_allows(matched: str, tally) -> bool:
    """Whether FUZZY_NAMES lets the matched name stand for the Tally name `tally`."""
    if FUZZY_NAMES == "always":
        return True
    if FUZZY_NAMES == "same_numbers":
        return _numbers(matched) == _numbers(tally)
    return False


def resolve_article(sku_name, particulars, match_score, exact_name) -> tuple[str, str]:
    """The article a Material In box carries on a transfer, and the rule that gave it; the
    first rule that applies wins:
      'exact'    `exact_name`: the item-master (all_sku) name that is the PO line's Tally
                 name (po_line.sku_name) with case and spaces ignored (_EXACT_SKU_SQL), as
                 stored;
      'matched'  the PO line's matched item-master name (po_line.particulars), whitespace
                 collapsed, when it is not blank, its match_score is at least MATCH_FLOOR
                 and FUZZY_NAMES allows it ("always"; "same_numbers": both names carry the
                 same numbers, so 'Cashew 320' is not 'Cashew W240'; "never");
      'tally'    the Tally name, whitespace collapsed ('' for none). Requests name
                 item-master items, so R1 refuses such a box on a request ("... which is
                 not on request R"); a transfer without a request takes it.
    po_line.match_source is never read: legacy uploads stored 'all_sku' for every match,
    and preview commits before 2026-09-04 stored the nearest master row whatever its
    score. The matched name can be missing even when an exact twin exists (the matcher
    compares case-sensitively), hence the exact rule first."""
    if (exact_name or "").strip():
        return exact_name, "exact"
    matched, score = _collapse(particulars), _score(match_score)
    if matched and score is not None and score >= MATCH_FLOOR and _fuzzy_allows(matched, sku_name):
        return matched, "matched"
    return _collapse(sku_name), "tally"


def _weight(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _lot(box_lot, section_lot) -> str | None:
    """A box's lot: its own, else its lot section's, trimmed; None when neither has one."""
    return (box_lot or "").strip() or (section_lot or "").strip() or None


def _company(entity) -> str:
    return "cdpl" if (entity or "").strip().lower() == "cdpl" else "cfpl"


def box_payload(row: dict, exact: dict | None = None) -> dict:
    """A Material In box (a _BOX_SQL row) as the box lookups return a box: the keys and
    types of lookup_service._box_from_row, plus keys the screens may ignore: source
    ('material_in'), company (the PO's), inward_site (the canonical name of the warehouse
    the PO was received into, or None), po_number, tally_name (the PO line's Tally name)
    and article_basis (resolve_article's rule). `exact` is the item-master row of the Tally
    name (_EXACT_SKU_SQL), or None; when it names the box, the box's category, sub
    category, material type and uom are that row's, else the PO line's. The lot is the
    box's, else its section's (_lot); weights are floats, 0.0 for none; box_number is the
    box's number in its lot section."""
    article, basis = resolve_article(row.get("sku_name"), row.get("particulars"), row.get("match_score"),
                                     exact.get("particulars") if exact else None)
    master = exact if basis == "exact" else None
    category = master.get("item_group") if master else row.get("item_category")
    sub_category = master.get("sub_group") if master else row.get("sub_category")
    material_type = master.get("item_type") if master else row.get("item_type")
    uom = master.get("uom") if master else row.get("uom")
    return {
        "box_id": row.get("box_id"),
        "transaction_no": row.get("transaction_no"),
        "box_number": row.get("box_number"),
        "article_description": article,
        "item_description": article,
        "sku_id": None,
        "item_category": category or "",
        "sub_category": sub_category or "",
        "material_type": (material_type or "").strip().upper() or None,
        "net_weight": _weight(row.get("net_weight")),
        "gross_weight": _weight(row.get("gross_weight")),
        "lot_number": _lot(row.get("box_lot"), row.get("section_lot")),
        "batch_number": "",
        "uom": None if uom is None else (str(uom).strip() or None),
        "quantity_units": None,
        "packaging_type": None,
        "quality_grade": None,
        "count": None,
        "source": "material_in",
        "company": _company(row.get("entity")),
        "inward_site": sites.canonical_site(row.get("warehouse")) or None,
        "po_number": row.get("po_number"),
        "tally_name": row.get("sku_name"),
        "article_basis": basis,
    }


async def _exact(conn, sku_name) -> dict | None:
    """The item-master row named by the Tally name `sku_name` (_EXACT_SKU_SQL), or None:
    also for no name, and without the all_sku table."""
    key = _collapse(sku_name).upper()
    if not key or not await _exists(conn, "all_sku"):
        return None
    row = await conn.fetchrow(_EXACT_SKU_SQL, key)
    return dict(row) if row else None


def _deleted_po(row: dict) -> str:
    tno = row.get("transaction_no")
    po = f"PO {row['po_number']} ({tno})" if (row.get("po_number") or "").strip() else f"PO {tno}"
    return (f"Box {row.get('box_id')} belongs to {po}, which was deleted in Purchase; it can't be sent. "
            "If the goods were entered again on another PO, use that PO's label.")


def _refuse_too_long(box: dict) -> None:
    """409 for a box whose lot or article is longer than the in-transit row holds (LOT_MAX,
    ARTICLE_MAX), which would otherwise fail the whole save."""
    lot, article = box["lot_number"] or "", box["item_description"] or ""
    if len(lot) > LOT_MAX:
        raise HTTPException(
            409, f"Box {box['box_id']}: its lot '{lot}' is longer than {LOT_MAX} characters, which a transfer "
                 "can't record; shorten it in Purchase > Material In.")
    if len(article) > ARTICLE_MAX:
        raise HTTPException(
            409, f"Box {box['box_id']}: its item name '{article[:40]}...' is longer than {ARTICLE_MAX} characters, "
                 "which a transfer can't record; shorten the item name in Purchase.")


async def find_box(conn, *, id_col: str, id_val, transaction_no) -> dict | None:
    """The Material In box `id_val` (its box_id, or with id_col 'box_number' its box
    number) of the PO `transaction_no`, as the box lookups return a box (box_payload), or
    None when there is none. The box id and transaction are matched trimmed (IMS's search,
    before this one, compares them as sent). 409 when the box can't be sent as its record
    stands:
      - its PO was deleted in Purchase (a box sent before that can still be kept on its
        transfer, and re-parks from snapshot);
      - its box number is carried by more than one box of the PO: box numbers restart in
        every lot section, so manual entry can't tell them apart (the QR label or the box
        ID can);
      - its lot is longer than LOT_MAX, or its article longer than ARTICLE_MAX.
    Read only: without the po tables, nothing but their existence probes."""
    match = _MATCH[id_col]
    if not await available(conn):
        return None
    tno = (transaction_no or "").strip()
    value = id_val.strip() if isinstance(id_val, str) else id_val
    if not tno or value is None or value == "":
        return None
    rows = [dict(r) for r in await conn.fetch(_BOX_SQL.format(match=match), value, tno)]
    if not rows:
        return None
    row = rows[0]
    if row.get("deleted_at") is not None:
        raise HTTPException(409, _deleted_po(row))
    if len(rows) > 1:
        raise HTTPException(
            409, f"Box #{value} of {tno} matches more than one Material In box (box numbers restart in every "
                 "lot section). Scan its QR label or enter its box ID.")
    box = box_payload(row, await _exact(conn, row.get("sku_name")))
    _refuse_too_long(box)
    return box


async def snapshot(conn, box_id, transaction_no) -> dict | None:
    """The Material In box (box_id + transaction_no, trimmed) as park_boxes copies it,
    its PO deleted or not (a box sent before its PO was deleted re-parks from it when its
    receipt is undone): {"id": None (po_box has no integer id), "_company": the PO's
    company, "article" (resolve_article), "lot_number" (the box's, else its section's),
    "net_weight" and "gross_weight" (as stored; None for none), "warehouse" (the PO's, as
    stored)}. None when there is no such box, or no po tables. Read only."""
    if not await available(conn):
        return None
    bid, tno = (box_id or "").strip(), (transaction_no or "").strip()
    if not bid or not tno:
        return None
    row = await conn.fetchrow(_BOX_SQL.format(match=_MATCH["box_id"]), bid, tno)
    if row is None:
        return None
    row = dict(row)
    box = box_payload(row, await _exact(conn, row.get("sku_name")))
    return {"id": None, "_company": box["company"], "article": box["item_description"],
            "lot_number": box["lot_number"], "net_weight": row.get("net_weight"),
            "gross_weight": row.get("gross_weight"), "warehouse": row.get("warehouse")}
