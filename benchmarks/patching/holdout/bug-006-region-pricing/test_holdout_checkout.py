from checkout import order_total
from shipping import shipping_cost
from tax import tax_rate


def test_mixed_case_regions():
    assert tax_rate("Tx") == 0.0625 and shipping_cost("tX") == 6.0


def test_total_for_every_case_variant():
    assert len({order_total(50.0, r) for r in ("NY", "ny", "Ny", "nY")}) == 1
