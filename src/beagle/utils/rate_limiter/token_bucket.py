import asyncio
import threading
import time


class TokenBucket:
    """Token bucket implementation for rate limiting.

    Uses __slots__ for memory efficiency and time.monotonic() for reliable timing.
    Supports both sync (threading.Lock) and async (asyncio.Lock) contexts.
    """

    __slots__ = (
        "_async_lock",
        "_lock",
        "capacity",
        "last_refill",
        "refill_rate",
        "tokens",
    )

    def __init__(self, capacity: float, refill_rate: float) -> None:
        """Initialize token bucket with full capacity."""
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._async_lock: asyncio.Lock | None = None

    def _get_async_lock(self) -> asyncio.Lock:
        """Lazily create asyncio.Lock (not picklable, so deferred)."""
        with self._lock:
            if self._async_lock is None:
                self._async_lock = asyncio.Lock()
            return self._async_lock

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            new_tokens = elapsed * self.refill_rate
            self.tokens = min(self.capacity, self.tokens + new_tokens)
            self.last_refill = now
        elif elapsed < 0:
            # Clock skew protection: don't allow negative elapsed time
            self.last_refill = now

    def consume(self, tokens: float = 1) -> bool:
        """
        Attempt to consume tokens from the bucket.

        Args:
            tokens: Number of tokens to consume

        Returns:
            True if tokens were consumed, False if insufficient tokens

        """
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def time_until_available(self, tokens: float = 1) -> float:
        """
        Calculate time until requested tokens are available.

        Args:
            tokens: Number of tokens needed

        Returns:
            Time in seconds until tokens are available (0 if immediately available)

        """
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                return 0.0
            needed = tokens - self.tokens
            return needed / self.refill_rate

    def available(self) -> float:
        """
        Get current number of available tokens.

        Returns:
            Number of tokens currently available

        """
        with self._lock:
            self._refill()
            return self.tokens

    async def async_consume(self, tokens: float = 1) -> bool:
        """Async version of consume — uses asyncio.Lock to avoid blocking the event loop."""
        async with self._get_async_lock():
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    async def async_time_until_available(self, tokens: float = 1) -> float:
        """Async version of time_until_available — uses asyncio.Lock."""
        async with self._get_async_lock():
            self._refill()
            if self.tokens >= tokens:
                return 0.0
            needed = tokens - self.tokens
            return needed / self.refill_rate

    async def async_available(self) -> float:
        """Async version of available — uses asyncio.Lock."""
        async with self._get_async_lock():
            self._refill()
            return self.tokens
