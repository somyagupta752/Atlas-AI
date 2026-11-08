def is_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is a later release than ``current``."""
    return candidate > current


def needs_update(installed: str, available: list[str]) -> bool:
    return any(is_newer(version, installed) for version in available)
