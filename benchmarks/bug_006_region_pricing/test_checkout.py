from checkout import order_total
from shipping import DEFAULT_SHIPPING, shipping_cost
from tax import tax_rate


def test_tax_rate_lowercase():
    assert tax_rate("ca") == 0.0725


def test_tax_rate_uppercase():
    assert tax_rate("CA") == 0.0725


def test_shipping_uppercase():
    assert shipping_cost("NY") == 7.5


def test_shipping_lowercase():
    assert shipping_cost("ny") == 7.5


def test_unknown_region():
    assert tax_rate("zz") == 0.0
    assert shipping_cost("zz") == DEFAULT_SHIPPING


def test_order_total_ignores_region_case():
    assert order_total(100.0, "CA") == order_total(100.0, "ca") == 112.25
