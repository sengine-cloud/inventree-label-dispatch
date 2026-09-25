"""Extractors are duck-typed, so plain stand-ins exercise them with no Django.

Dispatch is on the class *name*, so the fakes just need the right class name and the
attributes each extractor reads.
"""

from __future__ import annotations

from decimal import Decimal

from inventree_label_dispatch.extractors import extract


class StockItem:
    def __init__(self, pk, part, location=None, quantity=None, serial=None):
        self.pk, self.part, self.location = pk, part, location
        self.quantity, self.serial = quantity, serial


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


PHONE = Part(39, full_name="A1533 | iPhone 5s", name="iPhone 5s", IPN="A1533")


def test_serialised_stock_item_leads_with_short_code_and_ipn():
    out = extract(StockItem(1, PHONE, quantity=Decimal("1.00000"), serial="358814056238878"))
    assert out == {"code": "SI1", "title": "INV-SI1 · A1533", "sub": "iPhone 5s"}


def test_bulk_stock_shows_quantity_without_trailing_zeros():
    bolt = Part(7, name="M3x8 hex bolt", IPN="M3-8")
    out = extract(StockItem(4821, bolt, StockLocation(2, "BIN-A4", ""), Decimal("250.00000")))
    assert out["title"] == "INV-SI4821 · M3-8"
    assert out["sub"] == "M3x8 hex bolt · ×250"


def test_a_count_of_one_is_not_printed():
    out = extract(StockItem(5, Part(7, name="Bracket"), quantity=Decimal("1.00000")))
    assert out["sub"] == "Bracket"


def test_fractional_quantity_keeps_its_fraction():
    out = extract(StockItem(6, Part(8, name="Solder"), quantity=Decimal("1.50000")))
    assert out["sub"] == "Solder · ×1.5"


def test_part_uses_name_not_full_name():
    # full_name is "IPN | name"; the IPN is already in the headline.
    assert extract(PHONE) == {"code": "PA39", "title": "INV-PA39 · A1533", "sub": "iPhone 5s"}


def test_part_without_ipn_is_just_the_short_code():
    assert extract(Part(12, name="Widget"))["title"] == "INV-PA12"


def test_prefix_follows_the_instance_setting():
    assert extract(PHONE, "ACME-")["title"] == "ACME-PA39 · A1533"


def test_stock_location():
    out = extract(StockLocation(3, "Shelf 3", "Warehouse/Shelf 3"))
    assert out == {"code": "SL3", "title": "INV-SL3", "sub": "Shelf 3"}


def test_unknown_model_falls_back_to_pk():
    assert extract(Weird(99, "mystery"))["code"] == "PK99"
