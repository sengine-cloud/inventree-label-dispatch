"""Turn an InvenTree model instance into a preset's ``vars``.

Duck-typed on purpose: each extractor reads attributes, so a plain stand-in object
exercises it in a test without a Django database. The agent owns layout; these only
decide *what text* a label carries, keyed by a short scannable code.

Every label leads with the item's InvenTree short barcode (``INV-SI1``) and IPN, so
the text can be typed into InvenTree's scanner when the QR is unreadable. The name
goes underneath as context. ``code`` stays the bare ``SI1``: it is what the QR
encodes, behind the agent's short-link base, and every character there costs module
size on 12mm tape.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: InvenTree's default ``SHORT_BARCODE_PREFIX``. The driver passes the instance's real
#: setting; this only covers callers that have no InvenTree to ask.
DEFAULT_PREFIX = "INV-"


def _s(value) -> str:
    return "" if value is None else str(value)


def _qty(value) -> str:
    """``1.00000`` -> ``1`` and ``250.00000`` -> ``250``; ``1.5`` stays ``1.5``."""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return _s(value)


def _headline(short_code: str, ipn) -> str:
    ipn = _s(ipn)
    return f"{short_code} · {ipn}" if ipn else short_code


def extract_stock_item(item, prefix: str = DEFAULT_PREFIX) -> dict:
    part = getattr(item, "part", None)
    sub = _s(getattr(part, "name", "") or getattr(part, "full_name", ""))
    qty = getattr(item, "quantity", None)
    # A serialised item is exactly one by definition, and a count of one says nothing,
    # so the quantity only earns space on bulk stock.
    if qty is not None and not getattr(item, "serial", None) and _qty(qty) != "1":
        sub = f"{sub} · ×{_qty(qty)}".strip(" ·")
    code = f"SI{item.pk}"
    return {
        "code": code,
        "title": _headline(f"{prefix}{code}", getattr(part, "IPN", None)),
        "sub": sub,
    }


def extract_part(item, prefix: str = DEFAULT_PREFIX) -> dict:
    code = f"PA{item.pk}"
    return {
        "code": code,
        "title": _headline(f"{prefix}{code}", getattr(item, "IPN", None)),
        # ``name``, not ``full_name``: InvenTree builds full_name as "IPN | name", and
        # the IPN is already in the headline.
        "sub": _s(getattr(item, "name", "") or getattr(item, "full_name", "")),
    }


def extract_stock_location(item, prefix: str = DEFAULT_PREFIX) -> dict:
    code = f"SL{item.pk}"
    return {
        "code": code,
        "title": f"{prefix}{code}",
        "sub": _s(getattr(item, "name", "")),
    }


#: Model class name -> extractor. Matched by class name so no Django import is needed.
_BY_MODEL = {
    "StockItem": extract_stock_item,
    "Part": extract_part,
    "StockLocation": extract_stock_location,
}


def extract(item, prefix: str = DEFAULT_PREFIX) -> dict:
    """Best-effort extraction, dispatched on the item's model class name."""
    fn = _BY_MODEL.get(type(item).__name__)
    if fn is not None:
        return fn(item, prefix)
    # Unknown model: fall back to the primary key, so a label still prints something
    # scannable rather than the driver erroring on an unmapped type.
    return {"code": f"PK{getattr(item, 'pk', '?')}", "title": _s(getattr(item, 'name', '')), "sub": ""}
