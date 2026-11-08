import time

from durations import to_seconds


class TTLCache:
    """A small cache whose entries expire after a configured time to live."""

    def __init__(self, ttl: str = "5m", clock=time.monotonic) -> None:
        self.ttl_seconds = to_seconds(ttl)
        self._clock = clock
        self._items: dict[str, tuple[float, object]] = {}

    def set(self, key: str, value: object) -> None:
        self._items[key] = (self._clock(), value)

    def get(self, key: str) -> object | None:
        entry = self._items.get(key)
        if entry is None or self._clock() - entry[0] >= self.ttl_seconds:
            return None
        return entry[1]
