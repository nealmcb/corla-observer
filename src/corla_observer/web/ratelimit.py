"""In-memory sliding-window rate limits, per key (an address or an account).

One process holds the counts; a restart clears them. Behind Cloudflare,
Cloudflare's own rate limiting is the first line, and this the second.

Standard library only.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

#: Most keys tracked per limit; the least recently used are dropped beyond this.
MAX_KEYS = 10_000


@dataclass(frozen=True)
class Limit:
    """At most ``count`` events per ``seconds``."""

    count: int
    seconds: float


class RateLimiter:
    def __init__(self, limit: Limit, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Count one event for ``key``; False (without counting it) if over the limit."""
        now = self._clock()
        with self._lock:
            hits = self._hits.pop(key, None) or deque()
            while hits and now - hits[0] >= self.limit.seconds:
                hits.popleft()
            allowed = len(hits) < self.limit.count
            if allowed:
                hits.append(now)
            self._hits[key] = hits  # most recently used last
            while len(self._hits) > MAX_KEYS:
                del self._hits[next(iter(self._hits))]
            return allowed

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
