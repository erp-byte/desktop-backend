import logging
from dataclasses import dataclass

import asyncpg
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MasterItem:
    particulars: str
    item_type: str | None
    group: str | None
    sub_group: str | None
    uom: float | None
    sale_group: str | None
    gst: float | None


async def load_master_items(pool: asyncpg.Pool) -> list[MasterItem]:
    """Load all rows from all_sku into memory for fuzzy matching."""
    rows = await pool.fetch('SELECT * FROM all_sku')
    items = []
    for r in rows:
        items.append(MasterItem(
            particulars=r["particulars"] or "",
            item_type=r.get("item_type"),
            group=r.get("item_group"),
            sub_group=r.get("sub_group"),
            uom=float(r["uom"]) if r.get("uom") is not None else None,
            sale_group=r.get("sale_group"),
            gst=float(r["gst"]) if r.get("gst") is not None else None,
        ))
    logger.info("Loaded %d master items from all_sku", len(items))
    return items


def match_sku(
    sku_name: str,
    master_items: list[MasterItem],
    threshold: float = 75.0,
) -> tuple[MasterItem | None, float]:
    """
    Fuzzy-match a SKU name against master item particulars.
    Returns (matched_item, score_0_to_1) or (None, 0.0).
    """
    if not sku_name or not master_items:
        return None, 0.0

    query = sku_name.lower().strip()
    choices = [item.particulars for item in master_items]

    result = process.extractOne(
        query,
        choices,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=threshold,
    )

    if result is None:
        return None, 0.0

    _match_str, score, idx = result
    return master_items[idx], score / 100.0
