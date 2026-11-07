import pathlib
import re

import pytest

from calc import quote_total


def test_example():
    assert quote_total([(2, 150), (1, 99)]) == {"subtotal": 399, "vat": 80, "total": 479}


def test_empty():
    assert quote_total([]) == {"subtotal": 0, "vat": 0, "total": 0}


@pytest.mark.parametrize("pence,vat", [(2, 0), (3, 1), (12, 2), (13, 3), (1000, 200), (1238, 248), (7747, 1549)])
def test_rounding_half_up(pence, vat):
    r = quote_total([(1, pence)])
    assert r["vat"] == vat and r["total"] == pence + vat


def test_ints_not_floats():
    r = quote_total([(3, 333)])
    assert all(type(v) is int for v in r.values())


@pytest.mark.parametrize("lines", [[(-1, 100)], [(1, -100)]])
def test_negative_rejected(lines):
    with pytest.raises(ValueError):
        quote_total(lines)


def test_quote_page():
    html = pathlib.Path("quote.html").read_text()
    for money in ("3.99", "0.80", "4.79"):
        assert money in html
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=\"http", html)
