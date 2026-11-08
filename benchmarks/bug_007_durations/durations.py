"""Parse duration strings such as '90s', '5m' or '2h' into seconds."""

UNITS = {"s": 60, "m": 60, "h": 3600}


def to_seconds(value: str) -> int:
    value = value.strip().lower()
    number, unit = value[:-1], value[-1:]
    if unit not in UNITS or not number.isdigit():
        raise ValueError(f"invalid duration: {value!r}")
    return int(number) * UNITS[unit]
