"""A small in-memory sliding-window limiter for the authentication endpoints.

Per process: with several API workers each keeps its own counts, so the real limit is a multiple. The reverse
proxy (15.7) carries the shared limit. This stops password guessing against a single process.
"""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(self, attempts: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.attempts, self.window, self._clock = attempts, window_seconds, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str) -> deque[float]:
        hits = self._hits[key]
        cutoff = self._clock() - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def blocked(self, *keys: str) -> bool:
        with self._lock:
            return any(len(self._trim(k)) >= self.attempts for k in keys)

    def record(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._trim(k).append(self._clock())

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._hits.pop(k, None)

    def retry_after(self, *keys: str) -> int:
        with self._lock:
            waits = [self._hits[k][0] + self.window - self._clock() for k in keys if self._trim(k)]
        return max(1, int(max(waits, default=1)) + 1)
