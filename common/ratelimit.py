"""A small in-process rate limiter.

The OAuth server is a single waitress process, so a shared store such as Redis
would add a dependency, an operational component and a failure mode for state
that never needs to outlive the process. If Starguard is ever run as several
replicas behind one hostname this becomes per-replica, which is documented
rather than pretended away: the limit is a brake on scripted abuse of /login,
not an accounting system.

A sliding window is used rather than a fixed one. A fixed window lets twice
the limit through when the requests straddle the boundary, which for a limit
as small as this one is the difference between a working brake and a
decorative one.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

# Above this many distinct keys the table is swept, and if a sweep does not
# bring it back under the ceiling the least recently seen keys are dropped.
# An unbounded dict keyed by client address is itself a way to exhaust memory.
DEFAULT_MAX_KEYS: Final = 10000


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome of one :meth:`RateLimiter.hit`."""

    allowed: bool
    retry_after: int
    remaining: int


class RateLimiter:
    """Allow ``limit`` events per ``window_seconds`` for each key.

    ``clock`` is :func:`time.monotonic` so that a system clock adjustment
    cannot make an entry look arbitrarily old or arbitrarily fresh.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window = float(window_seconds)
        self._max_keys = max_keys
        self._clock = clock
        # waitress serves requests from a thread pool, so every read and write
        # of the table happens under this lock.
        self._lock = threading.Lock()
        # Each key keeps the timestamps of the events still inside its window,
        # oldest first, which is what makes expiry a popleft.
        self._hits: dict[str, deque[float]] = {}

    @property
    def limit(self) -> int:
        """The number of events allowed per window."""
        return self._limit

    def hit(self, key: str) -> RateLimitDecision:
        """Record an event for ``key`` and say whether it is allowed."""
        # The clock is read under the lock, not before it. Read outside, two
        # threads can take their timestamps in one order and reach the
        # append in the other, leaving a deque that is no longer oldest
        # first: the expiry loop stops at the first entry still inside the
        # window and leaves older ones behind it, and retry_after is
        # computed from an entry that is not the one about to expire.
        with self._lock:
            now = self._clock()
            cutoff = now - self._window

            if len(self._hits) > self._max_keys:
                self._evict(cutoff)

            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self._limit:
                retry_after = max(1, math.ceil(hits[0] + self._window - now))
                return RateLimitDecision(False, retry_after, 0)

            hits.append(now)
            return RateLimitDecision(True, 0, self._limit - len(hits))

    def _evict(self, cutoff: float) -> None:
        """Drop expired keys, then the oldest ones if that was not enough."""
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

        excess = len(self._hits) - self._max_keys
        if excess <= 0:
            return

        # A genuine flood of distinct addresses. Dropping the least recently
        # seen keys lets some requests through that should have been refused,
        # which is the right trade against growing the table without limit.
        oldest = sorted(self._hits, key=lambda k: self._hits[k][-1])[:excess]
        for key in oldest:
            del self._hits[key]
