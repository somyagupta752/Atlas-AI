from cache import TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_minutes_ttl():
    assert TTLCache("5m").ttl_seconds == 300


def test_seconds_ttl():
    assert TTLCache("90s").ttl_seconds == 90


def test_entry_expires_after_ttl():
    clock = FakeClock()
    cache = TTLCache("90s", clock=clock)
    cache.set("k", "v")
    clock.now = 89
    assert cache.get("k") == "v"
    clock.now = 91
    assert cache.get("k") is None
