"""Extractors are duck-typed, so plain stand-ins exercise them with no Django.

Dispatch is on the class *name*, so the fakes just need the right class name and the
attributes each extractor reads.
"""

from __future__ import annotations

from inventree_label_dispatch.extractors import extract


class StockItem:
    def __init__(self, pk, part, location, quantity):
        self.pk, self.part, self.location, self.quantity = pk, part, location, quantity


class Part:
    def __init__(self, pk, full_name=None, name=None, IPN=None, description=None):
        self.pk = pk
        self.full_name = full_name
        self.name = name
        self.IPN = IPN
        self.description = description


class StockLocation:
    def __init__(self, pk, name, pathstring):
        self.pk, self.name, self.pathstring = pk, name, pathstring


class Weird:
    def __init__(self, pk, name):
        self.pk, self.name = pk, name


def test_stock_item():
    item = StockItem(4821, Part(1, full_name="M3x8 hex bolt"), StockLocation(2, "BIN-A4", ""), 250)
    out = extract(item)
    assert out["code"] == "SI4821"
    assert out["title"] == "M3x8 hex bolt"
    assert "BIN-A4" in out["sub"] and "250" in out["sub"]


def test_part():
    out = extract(Part(12, full_name="Widget", IPN="WID-001"))
    assert out == {"code": "PA12", "title": "Widget", "sub": "WID-001"}


def test_stock_location():
    out = extract(StockLocation(3, "Shelf 3", "Warehouse/Shelf 3"))
    assert out == {"code": "SL3", "title": "Shelf 3", "sub": "Warehouse/Shelf 3"}


def test_unknown_model_falls_back_to_pk():
    assert extract(Weird(99, "mystery"))["code"] == "PK99"
