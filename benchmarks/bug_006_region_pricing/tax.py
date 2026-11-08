"""Sales tax by region. Region codes are case-insensitive: "CA" and "ca" are the same region."""

TAX_RATES = {"ca": 0.0725, "ny": 0.04, "tx": 0.0625}


def tax_rate(region: str) -> float:
    return TAX_RATES.get(region, 0.0)
