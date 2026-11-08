from shipping import shipping_cost
from tax import tax_rate


def order_total(subtotal: float, region: str) -> float:
    """Subtotal plus sales tax plus shipping, rounded to cents."""
    return round(subtotal * (1 + tax_rate(region)) + shipping_cost(region), 2)
