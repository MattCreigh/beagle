import threading
import time
from typing import Any

from .config import RateLimitConfig
from .token_bucket import TokenBucket


class RateLimiter:
    """Base rate limiter using token bucket algorithm."""

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        """Initialize rate limiter with configuration."""
        self.config = config or RateLimitConfig()
        self.bucket = TokenBucket(
            capacity=float(self.config.burst_size),
            refill_rate=self.config.requests_per_second,
        )
        self._request_count = 0
        self._blocked_count = 0
        self._lock = threading.Lock()

    def check_request(self) -> bool:
        """Check if a request would be allowed without consuming quota."""
        return self.bucket.available() >= 1

    def check_tokens(self, tokens: int) -> bool:
        """Check if tokens would be available without consuming quota."""
        return self.bucket.available() >= tokens

    def consume_request(self) -> bool:
        """Consume quota for a single request."""
        result = self.bucket.consume(1)
        if result:
            self._request_count += 1
        return result

    def consume_tokens(self, tokens: int) -> bool:
        """Consume quota for specified number of tokens."""
        return self.bucket.consume(tokens)

    def wait_for_request(self, block: bool = True) -> float:
        """
        Wait for request quota to be available.

        Args:
            block: Whether to block until available or return immediately

        Returns:
            Time waited in seconds

        """
        if not block:
            return 0.0 if self.check_request() else -1.0

        wait_time = self.bucket.time_until_available(1)
        if wait_time > 0:
            time.sleep(wait_time)
        return wait_time

    def wait_for_tokens(self, tokens: int, block: bool = True) -> float:
        """
        Wait for token quota to be available.

        Args:
            tokens: Number of tokens needed
            block: Whether to block until available or return immediately

        Returns:
            Time waited in seconds

        """
        if not block:
            return 0.0 if self.check_tokens(tokens) else -1.0

        wait_time = self.bucket.time_until_available(tokens)
        if wait_time > 0:
            time.sleep(wait_time)
        return wait_time

    async def acquire(self, estimated_tokens: int = 1000) -> float:
        """
        Acquire quota for a request with estimated token usage.

        Args:
            estimated_tokens: Estimated number of tokens for the request

        Returns:
            Time waited in seconds (0.0 for immediate, >0 for delayed, -1.0 for blocked)

        """
        import asyncio

        # First check: can we acquire immediately?
        with self._lock:
            if self.bucket.available() >= estimated_tokens:
                self.bucket.consume(estimated_tokens)
                self._request_count += 1
                return 0.0  # Immediate success

        # Need to wait — compute how long and async-sleep
        wait_time = self.bucket.time_until_available(estimated_tokens)
        if wait_time > 0:
            await asyncio.sleep(wait_time)

        # After waiting, try to acquire under lock (may still fail if others consumed first)
        with self._lock:
            if self.bucket.available() >= estimated_tokens:
                self.bucket.consume(estimated_tokens)
                self._request_count += 1
                return wait_time
            else:
                self._blocked_count += 1
                return -1.0

    def apply_backpressure(self, reduction_factor: float = 0.5) -> None:
        """
        Apply backpressure by temporarily reducing available tokens.

        Args:
            reduction_factor: Factor to reduce tokens by (0.0 to 1.0)

        Raises:
            ValueError: If reduction_factor is outside valid range [0.0, 1.0]

        """
        if not 0.0 <= reduction_factor <= 1.0:
            raise ValueError(
                f"reduction_factor must be between 0.0 and 1.0, got {reduction_factor}"
            )
        with self._lock:
            self.bucket.tokens = max(0, self.bucket.tokens * (1 - reduction_factor))

    def stats(self) -> dict[str, Any]:
        """Get rate limiter statistics."""
        with self._lock:
            return {
                "requests": {
                    "total": self._request_count,
                    "blocked": self._blocked_count,
                },
                "tokens": {
                    "available": self.bucket.available(),
                    "capacity": self.bucket.capacity,
                },
                "utilization": 1.0 - (self.bucket.available() / self.bucket.capacity)
                if self.bucket.capacity > 0
                else 0.0,
            }
