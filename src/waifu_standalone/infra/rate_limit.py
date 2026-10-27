"""Token buckets for tools that cost money or call outside services.

A call may need a token from several buckets at once (the sender's and the
group's); it gets all of them or none. Buckets live in memory, are bounded, and
start full, so a restart or an evicted bucket only ever grants more, never less.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from typing import Callable, Iterable

DEFAULT_MAX_BUCKETS = 10_000


class TokenBucketLimiter:
    def __init__(
        self,
        *,
        max_buckets: int = DEFAULT_MAX_BUCKETS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_buckets = max(1, int(max_buckets))
        self._clock = clock
        # key -> (tokens, last refill time)
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def acquire(self, rules: Iterable[tuple[str, float, float]]) -> float:
        """Take one token from every bucket in ``rules``, or from none.

        Each rule is ``(key, capacity, refill_per_second)``; a capacity of 0 or
        less leaves that bucket out. Returns 0.0 when granted, otherwise the
        seconds until all buckets have a token again (``math.inf`` if a bucket
        never refills).
        """
        now = self._clock()
        with self._lock:
            refreshed: list[tuple[str, float]] = []
            wait = 0.0
            for key, capacity, refill_per_second in rules:
                if capacity <= 0:
                    continue
                tokens, updated_at = self._buckets.get(key, (float(capacity), now))
                tokens = min(float(capacity), tokens + max(0.0, now - updated_at) * max(0.0, refill_per_second))
                refreshed.append((key, tokens))
                if tokens < 1.0:
                    missing = 1.0 - tokens
                    wait = max(wait, missing / refill_per_second if refill_per_second > 0 else math.inf)
            spend = 0.0 if wait > 0 else 1.0
            for key, tokens in refreshed:
                self._buckets[key] = (tokens - spend, now)
                self._buckets.move_to_end(key)
            while len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)
            return wait
