import pytest
from pricing import apply_discount, order_total


def test_no_discount():
    assert apply_discount(50.0, 0) == 50.0


def test_ten_percent_discount():
    assert apply_discount(100.0, 10) == 90.0


def test_order_total_with_discount():
    assert order_total([10.0, 20.0], 50) == 15.0


def test_invalid_percent():
    with pytest.raises(ValueError):
        apply_discount(10.0, 150)
