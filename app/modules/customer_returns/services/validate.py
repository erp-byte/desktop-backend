"""What a customer return's header, lines and boxes must hold before they are saved.

The screens check these too, but the server is where they hold: through the API a
blank customer, a misspelt factory unit, text where a number goes, or a negative
weight used to be stored as it came (blank, 0, or the text dropped). Every refusal
here is a 422 with the module's {error, message, details} shape, naming the field
and the article, so a screen can show it as it is:
  * header: customer and factory unit can't be blank; the factory unit must be a
    site the ERP knows (app.core.sites), in any of its spellings. An edit that
    leaves an old return's unknown unit as it was is allowed;
  * lines: article and UOM can't be blank, and an article appears once. Quantity
    is a whole number (the user's call, 31 Oct: refuse a decimal rather than cut
    it, as the save used to); quantity, rate, net weight and carton weight are
    numbers, never negative. "1,200" reads as 1200. The value is always quantity x
    rate, worked out here: a value sent by the browser is not taken;
  * boxes: net and gross weight and count are never negative, count is whole, and
    a box's net weight can't be above its gross weight once the gross is weighed.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from app.core.sites import same_site, site_kind

_GROUPED = re.compile(r"^[+-]?\d{1,3}(,\d{2,3})+(\.\d+)?$")   # 1,200 / 1,20,000.5


def refuse(error: str, message: str, **details) -> HTTPException:
    return HTTPException(422, detail={"error": error, "message": message, "details": details})


def number(raw, what: str, *, whole: bool = False) -> Decimal:
    """A non-negative number from what the screen sent; blank is 0."""
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return Decimal(0)
    if _GROUPED.match(text):
        text = text.replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise refuse("invalid_number", f"{what} must be a number, not “{raw}”.", field=what, value=str(raw))
    if not value.is_finite():
        raise refuse("invalid_number", f"{what} must be a number, not “{raw}”.", field=what, value=str(raw))
    if value < 0:
        raise refuse("negative_number", f"{what} can't be negative ({raw}).", field=what, value=str(raw))
    if whole and value != value.to_integral_value():
        raise refuse("whole_number_only", f"{what} must be a whole number ({raw}).", field=what, value=str(raw))
    return value


def _blank(v) -> bool:
    return not str(v or "").strip()


def factory_unit(unit, *, stored=None) -> None:
    """A known site, unless it is the stored unit left as it was."""
    if _blank(unit):
        raise refuse("required", "Factory unit can't be blank.", field="factory_unit")
    if site_kind(str(unit)) != "unknown":
        return
    if stored is not None and (str(unit).strip() == str(stored).strip() or same_site(unit, stored)):
        return
    raise refuse("unknown_factory_unit", f"“{unit}” is not a factory unit the ERP knows. Pick one from the list.",
                 field="factory_unit", value=str(unit))


def header(h, *, stored_unit=None, partial: bool = False) -> None:
    """A create's header (every field), or an edit's (only the fields it sends)."""
    fields = h.model_dump(exclude_none=partial)
    if "customer" in fields and _blank(fields["customer"]):
        raise refuse("required", "Customer can't be blank.", field="customer")
    if "factory_unit" in fields:
        factory_unit(fields["factory_unit"], stored=stored_unit)
    if fields.get("conversion") is not None:
        number(fields["conversion"], "Conversion")


def lines(items) -> list[dict]:
    """Each line's numbers, checked: {qty: int, rate, value, net_weight, carton_weight}."""
    seen: dict[str, str] = {}
    out = []
    for n, line in enumerate(items, 1):
        article = (line.item_description or "").strip()
        if not article:
            raise refuse("required", f"Line {n} has no article.", field="item_description", line=n)
        key = " ".join(article.split()).casefold()
        if key in seen:
            raise refuse("duplicate_article", f"{article} is on the return twice. Put it on one line.",
                         field="item_description", article=article)
        seen[key] = article
        if _blank(line.uom):
            raise refuse("required", f"{article} has no UOM.", field="uom", article=article)
        qty = number(line.qty, f"Quantity of {article}", whole=True)
        rate = number(line.rate, f"Rate of {article}")
        out.append({
            "qty": int(qty),
            "rate": float(rate),
            "value": float(qty * rate),
            "net_weight": float(number(line.net_weight, f"Net weight of {article}")),
            "carton_weight": float(number(line.carton_weight, f"Carton weight of {article}")),
        })
    return out


def box(b) -> None:
    """One box's weights and count (a print or one item of a box save)."""
    what = f"Box {b.box_number} of {b.article_description}"
    for field, label in (("net_weight", "net weight"), ("gross_weight", "gross weight")):
        v = getattr(b, field)
        if v is not None and v < 0:
            raise refuse("negative_number", f"{what}: {label} can't be negative ({v}).",
                         field=field, article=b.article_description, box_number=b.box_number)
    if b.count is not None and b.count < 0:
        raise refuse("negative_number", f"{what}: count can't be negative ({b.count}).",
                     field="count", article=b.article_description, box_number=b.box_number)
    net, gross = b.net_weight, b.gross_weight
    if net is not None and gross is not None and gross > 0 and net > gross:
        raise refuse("net_above_gross", f"{what}: net weight {net} is more than gross weight {gross}.",
                     field="net_weight", article=b.article_description, box_number=b.box_number)
