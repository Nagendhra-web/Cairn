"""Async token-bucket rate limiter and circuit breaker."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    """Classic token bucket: ``rate`` tokens per second, burst of ``capacity``.

    ``acquire`` waits until enough tokens are available; ``try_acquire`` never
    waits and is used by the HTTP API to reject excess requests with 429.
    """

    def __init__(
        self, rate: float, capacity: float | None = None, clock: Callable[[], float] | None = None
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._clock = clock or time.monotonic
        self._tokens = self.capacity
        self._updated = self._clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens > self.capacity:
            raise ValueError("requested more tokens than bucket capacity")
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self.rate)


class KeyedRateLimiter:
    """One bucket per key (API key, tenant, provider)."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(self.rate, self.capacity)
        return self._buckets[key]

    def try_acquire(self, key: str, tokens: float = 1.0) -> bool:
        return self.bucket(key).try_acquire(tokens)


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures for ``cooldown_s``.

    While open, the router skips the endpoint and falls back immediately
    instead of waiting on a provider that is down.
    """

    def __init__(
        self, threshold: int = 3, cooldown_s: float = 30.0, clock: Callable[[], float] | None = None
    ) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock or time.monotonic
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def open(self) -> bool:
        if self.opened_at is None:
            return False
        if self._clock() - self.opened_at >= self.cooldown_s:
            # Half-open: allow one trial request through.
            self.opened_at = None
            self.failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self._clock()
