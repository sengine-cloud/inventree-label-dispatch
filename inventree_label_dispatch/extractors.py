"""Turn an InvenTree model instance into a preset's ``vars``.

Duck-typed on purpose: each extractor reads attributes, so a plain stand-in object
exercises it in a test without a Django database. The agent owns layout; these only
decide *what text* a label carries, keyed by a short scannable code.
"""

from __future__ import annotations


def _s(value) -> str:
    return "" if value is None else str(value)


def extract_stock_item(item) -> dict:
    part = getattr(item, "part", None)
    location = getattr(item, "location", None)
    qty = getattr(item, "quantity", None)
    sub = _s(getattr(location, "name", "")) if location else ""
    if qty is not None:
        sub = f"{sub} · {qty}".strip(" ·")
    return {
        "code": f"SI{item.pk}",
        "title": _s(getattr(part, "full_name", None) or getattr(part, "name", "")),
        "sub": sub,
    }


def extract_part(item) -> dict:
    return {
        "code": f"PA{item.pk}",
        "title": _s(getattr(item, "full_name", None) or getattr(item, "name", "")),
        "sub": _s(getattr(item, "IPN", "") or getattr(item, "description", "")),
    }


def extract_stock_location(item) -> dict:
    return {
        "code": f"SL{item.pk}",
        "title": _s(getattr(item, "name", "")),
        "sub": _s(getattr(item, "pathstring", "")),
    }


#: Model class name -> extractor. Matched by class name so no Django import is needed.
_BY_MODEL = {
    "StockItem": extract_stock_item,
    "Part": extract_part,
    "StockLocation": extract_stock_location,
}


def extract(item) -> dict:
    """Best-effort extraction, dispatched on the item's model class name."""
    fn = _BY_MODEL.get(type(item).__name__)
    if fn is not None:
        return fn(item)
    # Unknown model: fall back to the primary key, so a label still prints something
    # scannable rather than the driver erroring on an unmapped type.
    return {"code": f"PK{getattr(item, 'pk', '?')}", "title": _s(getattr(item, 'name', '')), "sub": ""}
