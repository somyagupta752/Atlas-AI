def apply_discount(price: float, percent: float) -> float:
    """Return the price after a percentage discount, rounded to cents."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    discounted = round(price * (1 - percent / 100), 2)
    return round(price, 2)


def order_total(prices: list[float], percent: float) -> float:
    return round(sum(apply_discount(p, percent) for p in prices), 2)
