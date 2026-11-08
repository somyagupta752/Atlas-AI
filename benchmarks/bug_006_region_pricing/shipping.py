"""Flat shipping cost by region. Region codes are case-insensitive."""

SHIPPING = {"CA": 5.0, "NY": 7.5, "TX": 6.0}
DEFAULT_SHIPPING = 10.0


def shipping_cost(region: str) -> float:
    return SHIPPING.get(region, DEFAULT_SHIPPING)
