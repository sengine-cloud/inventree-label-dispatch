"""Short-link parsing: what the QR on a label resolves to, with no InvenTree needed."""

from __future__ import annotations

import pytest

from inventree_label_dispatch.shortlink import parse

BASE = "https://sngn.top/i/"


@pytest.mark.parametrize(
    "data, expected",
    [
        ("https://sngn.top/i/SI1", ("SI", 1)),
        # Labels printed before the scheme was added: bare and uppercase.
        ("SNGN.TOP/I/PA39", ("PA", 39)),
        ("http://sngn.top/i/sl5", ("SL", 5)),
        ("https://sngn.top/i/PA39/", ("PA", 39)),
        ("  https://sngn.top/i/SI1\n", ("SI", 1)),
    ],
)
def test_matches(data, expected):
    assert parse(data, BASE) == expected


@pytest.mark.parametrize(
    "data",
    [
        "INV-SI1",  # InvenTree's own format; the builtin plugin handles it
        "SI1",  # a bare code is not a link; only the base makes it one
        "https://example.com/i/SI1",
        "https://sngn.top.example.com/i/SI1",
        "https://sngn.top/x/SI1",
        "https://sngn.top/i/",
        "https://sngn.top/i/SI",
        "https://sngn.top/i/S1",
        "https://sngn.top/i/SI1x",
        "https://sngn.top/i/SI1?utm=x",
        "",
    ],
)
def test_rejects(data):
    assert parse(data, BASE) is None


def test_base_scheme_and_trailing_slash_are_optional():
    assert parse("https://sngn.top/i/SI1", "sngn.top/i") == ("SI", 1)


def test_empty_base_disables_matching():
    assert parse("https://sngn.top/i/SI1", "") is None


@pytest.mark.parametrize("data", [None, {"stockitem": 1}, 42])
def test_non_string_data_is_ignored(data):
    assert parse(data, BASE) is None
