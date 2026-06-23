"""Token-bucket rate limiter for DingTalk API calls.

DingTalk API has QPS limits. This limiter ensures we stay within bounds
by acquiring tokens before making API calls.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Simple token-bucket rate limiter.

    Args:
        max_qps: Maximum queries per second.
        burst: Maximum burst size (defaults to max_qps).
    """

    def __init__(self, max_qps: float = 20, burst: int | None = None) -> None:
        self._max_qps = max_qps
        self._burst = burst or int(max_qps)
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                # Time until next token
                wait = 1.0 / self._max_qps
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._burst,
            self._tokens + elapsed * self._max_qps,
        )
        self._last_refill = now

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        pass
