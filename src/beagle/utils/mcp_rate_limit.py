"""
Shared MCP rate limiter for RAG, Utility, and OpenClaw servers.

Uses a sliding-window deque with asyncio.Lock for concurrent safety.
Per-process limiter — matches existing single-process MCP semantics.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimitExceeded(RuntimeError):
    """Raised when the MCP rate limit budget is exhausted."""


class RateLimiter:
    """Sliding-window rate limiter with async lock protection.

    Args:
        max_calls: Maximum calls allowed within the window.
        window_seconds: Duration of the sliding window in seconds.

    """

    def __init__(self, max_calls: int = 120, window_seconds: float = 60.0) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def check(self) -> None:
        """Check if a call is within budget. Raises RateLimitExceeded if not.

        Must be called with ``await`` inside an async context.
        """
        async with self._lock:
            now = time.time()
            # Prune expired timestamps
            while self._timestamps and now - self._timestamps[0] > self._window_seconds:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max_calls:
                raise RateLimitExceeded(
                    f"MCP rate limit exceeded: {self._max_calls} "
                    f"calls per {self._window_seconds}s window"
                )
            self._timestamps.append(now)
