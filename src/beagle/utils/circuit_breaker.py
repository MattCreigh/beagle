"""Circuit Breaker pattern implementation for Beagle v12.0.

Prevents cascade failures when LLM API calls or subprocess executions
repeatedly fail. Once a threshold is exceeded, the circuit "opens"
and fast-fails without attempting execution.

Based on recommendations from self-improvement report (R1).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

logger = __import__("logging").getLogger("Beagle.circuit_breaker")

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, requests pass through
    OPEN = "open"  # Failing, fast-fail without attempting
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""

    failure_threshold: int = 5  # Failures before opening circuit
    success_threshold: int = 3  # Successes in HALF_OPEN to close
    timeout_seconds: float = 30.0  # Seconds before trying HALF_OPEN
    half_open_max_calls: int = 1  # Max concurrent calls in HALF_OPEN


@dataclass
class CircuitBreakerStats:
    """Statistics for monitoring circuit breaker health."""

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    state_changes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    consecutive_failures: int = 0
    consecutive_successes: int = 0


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open and request is rejected."""

    def __init__(self, circuit_name: str, retry_after: float):
        self.circuit_name = circuit_name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker '{circuit_name}' is OPEN. Retry after {retry_after:.1f}s."
        )


class CircuitBreaker:
    """Circuit breaker for protecting against cascade failures.

    States:
    - CLOSED: Normal operation. Failures increment counter.
    - OPEN: Circuit tripped. All calls fast-fail immediately.
    - HALF_OPEN: Testing recovery. Limited calls allowed.

    Usage:
        cb = CircuitBreaker("llm-api", failure_threshold=5)

        async with cb:
            result = await call_llm_api()
    """

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._stats = CircuitBreakerStats()
        self._lock = asyncio.Lock()
        self._last_state_change = time.monotonic()

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        """Get circuit breaker statistics."""
        return self._stats

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        return self._state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (fast-failing)."""
        return self._state == CircuitState.OPEN

    async def _can_attempt(self) -> bool:
        """Check if a call should be attempted."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if timeout has elapsed
                elapsed = time.monotonic() - self._last_state_change
                if elapsed >= self.config.timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._stats.state_changes += 1
                    self._last_state_change = time.monotonic()
                    logger.info(f"[{self.name}] Circuit HALF_OPEN after {elapsed:.1f}s timeout")
                    return True
                return False

            # HALF_OPEN state - allow limited calls
            return True

    async def _record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            self._stats.successful_calls += 1
            self._stats.consecutive_successes += 1
            self._stats.consecutive_failures = 0
            self._stats.last_success_time = time.monotonic()

            if (
                self._state == CircuitState.HALF_OPEN
                and self._stats.consecutive_successes >= self.config.success_threshold
            ):
                self._state = CircuitState.CLOSED
                self._stats.consecutive_successes = 0
                self._stats.state_changes += 1
                self._last_state_change = time.monotonic()
                logger.info(
                    f"[{self.name}] Circuit CLOSED after {self._stats.successful_calls} successes"
                )

    async def _record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self._stats.failed_calls += 1
            self._stats.consecutive_failures += 1
            self._stats.last_failure_time = time.monotonic()
            self._stats.consecutive_successes = 0

            if self._state == CircuitState.CLOSED:
                if self._stats.consecutive_failures >= self.config.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._stats.state_changes += 1
                    self._last_state_change = time.monotonic()
                    logger.warning(
                        f"[{self.name}] Circuit OPENED after "
                        f"{self._stats.consecutive_failures} consecutive failures"
                    )
            elif self._state == CircuitState.HALF_OPEN:
                # Any failure in HALF_OPEN reopens the circuit
                self._state = CircuitState.OPEN
                self._stats.state_changes += 1
                self._last_state_change = time.monotonic()
                logger.warning(f"[{self.name}] Circuit REOPENED from HALF_OPEN")

    def get_retry_after(self) -> float:
        """Get seconds until circuit might close."""
        elapsed = time.monotonic() - self._last_state_change
        return max(0.0, self.config.timeout_seconds - elapsed)

    def get_cooldown_info(self) -> dict:
        """Get detailed cooldown information for retry guidance.

        Returns:
            Dict with cooldown state for intelligent backoff

        """
        elapsed = time.monotonic() - self._last_state_change
        remaining = max(0.0, self.config.timeout_seconds - elapsed)
        progress = min(1.0, elapsed / self.config.timeout_seconds)

        return {
            "elapsed_seconds": elapsed,
            "remaining_seconds": remaining,
            "progress_percent": round(progress * 100, 1),
            "can_retry_soon": remaining < 5.0,  # Within 5 seconds
            "recommended_backoff": self._get_recommended_backoff(),
        }

    def _get_recommended_backoff(self) -> float:
        """Get recommended backoff based on circuit state."""
        if self._state == CircuitState.CLOSED:
            return 1.0  # No backoff needed
        elif self._state == CircuitState.OPEN:
            # Longer backoff while open
            return min(self.config.timeout_seconds * 1.5, 120.0)
        else:  # HALF_OPEN
            return self.config.timeout_seconds * 0.5

    async def call(
        self,
        func: Callable[..., T],
        *args,
        **kwargs,
    ) -> T:
        """Execute a function with circuit breaker protection.

        Args:
            func: Async function to call
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result of func

        Raises:
            CircuitBreakerOpenError: If circuit is open

        """
        self._stats.total_calls += 1

        if not await self._can_attempt():
            self._stats.rejected_calls += 1
            raise CircuitBreakerOpenError(self.name, self.get_retry_after())

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            await self._record_success()
            return result  # type: ignore[no-any-return]
        except Exception:  # broad catch intentional
            await self._record_failure()
            raise

    async def __aenter__(self) -> CircuitBreaker:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, _exc_tb) -> bool:
        """Async context manager exit."""
        if exc_type is not None and exc_val is not None:
            await self._record_failure()
        else:
            await self._record_success()
        return False  # Don't suppress exceptions

    # v13.22.3: Public aliases for the private _record_success /
    # _record_failure methods. Several callers (notably
    # utils/subprocess/execution.py) call circuit.record_success() /
    # record_failure() directly when managing state outside the
    # `call()` / `__aexit__` path. The private methods were the only
    # implementation; the public names did not exist, so every
    # success/failure recording raised AttributeError and masked the
    # underlying result. Expose the same behaviour under both names.
    async def record_success(self) -> None:
        """Public alias for ``_record_success``.

        Record a successful call. Safe to invoke from any caller that
        owns a CircuitBreaker reference but is not using it as a
        context manager or via ``call()``.
        """
        await self._record_success()

    async def record_failure(self) -> None:
        """Public alias for ``_record_failure``.

        Record a failed call. Safe to invoke from any caller that
        owns a CircuitBreaker reference but is not using it as a
        context manager or via ``call()``.
        """
        await self._record_failure()

    def get_health_report(self) -> dict:
        """Get health status for monitoring."""
        return {
            "name": self.name,
            "state": self._state.value,
            "stats": {
                "total_calls": self._stats.total_calls,
                "successful_calls": self._stats.successful_calls,
                "failed_calls": self._stats.failed_calls,
                "rejected_calls": self._stats.rejected_calls,
                "consecutive_failures": self._stats.consecutive_failures,
                "state_changes": self._stats.state_changes,
            },
            "health_score": self._calculate_health_score(),
            "retry_after_seconds": self.get_retry_after(),
        }

    def _calculate_health_score(self) -> float:
        """Calculate health score 0.0-1.0."""
        if self._stats.total_calls == 0:
            return 1.0

        # Penalize for failures and rejections
        failure_ratio = self._stats.failed_calls / self._stats.total_calls
        rejection_ratio = self._stats.rejected_calls / self._stats.total_calls

        score = 1.0 - (failure_ratio * 0.7 + rejection_ratio * 0.3)
        return max(0.0, min(1.0, score))


# ── Global circuit breakers for different services ────────────────────────────

_MAX_CIRCUITS = 100  # Maximum number of named circuit breakers to prevent unbounded growth

_circuits: dict[str, CircuitBreaker] = {}
_circuits_lock: asyncio.Lock | None = (
    None  # Lazy-init: asyncio.Lock must not be created before event loop
)
_circuits_sync_lock = threading.Lock()  # v0.3.0: for sync access in health report


def _get_circuits_lock() -> asyncio.Lock:
    """Lazily create the async circuits lock inside a running event loop."""
    global _circuits_lock
    if _circuits_lock is None:
        with _circuits_sync_lock:
            if _circuits_lock is None:
                _circuits_lock = asyncio.Lock()
    return _circuits_lock


def _evict_oldest_circuit() -> None:
    """Evict the oldest circuit breaker when _MAX_CIRCUITS is reached.

    Uses insertion-order tracking to remove the least-recently-used circuit.
    This prevents unbounded memory growth from unlimited circuit creation.
    """
    if len(_circuits) >= _MAX_CIRCUITS:
        # Remove the oldest entry (first key inserted)
        oldest_key = next(iter(_circuits), None)
        if oldest_key is not None:
            logger.info(
                f"[CircuitBreaker] Evicting oldest circuit '{oldest_key}' (limit: {_MAX_CIRCUITS})"
            )
            del _circuits[oldest_key]


async def get_circuit_breaker(
    name: str, config: CircuitBreakerConfig | None = None
) -> CircuitBreaker:
    """Get or create a named circuit breaker.

    Args:
        name: Unique name for the circuit (e.g., "llm-api", "subprocess")
        config: Optional configuration

    Returns:
        CircuitBreaker instance

    """
    global _circuits
    async with _get_circuits_lock():
        with _circuits_sync_lock:
            if name not in _circuits:
                _evict_oldest_circuit()  # Enforce _MAX_CIRCUITS limit
                _circuits[name] = CircuitBreaker(name, config)
            return _circuits[name]


async def get_all_circuits() -> dict[str, CircuitBreaker]:
    """Get all registered circuit breakers."""
    async with _get_circuits_lock():
        return dict(_circuits)


async def reset_circuit(name: str | None = None) -> None:
    """Reset circuit breaker(s).

    Args:
        name: Optional specific circuit name. If None, resets all.

    """
    global _circuits
    async with _get_circuits_lock():
        if name:
            if name in _circuits:
                _circuits[name] = CircuitBreaker(name, _circuits[name].config)
        else:
            _circuits.clear()


def get_circuit_health_report() -> dict:
    """Get health report for all circuits.

    v0.3.0: Thread-safe snapshot to avoid race with async circuit creation.
    """
    with _circuits_sync_lock:
        snapshot = dict(_circuits)
    return {name: cb.get_health_report() for name, cb in snapshot.items()}


class LLMCircuitBreaker(CircuitBreaker):
    """Circuit breaker with LLM-specific signals.

    Monitors:
    - Token consumption rate (tokens/sec)
    - p95 latency of LLM responses
    - Consecutive 429 rate-limit responses
    - Semantic degradation (empty/repeated/malformed responses)
    """

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
        token_rate_threshold: float = 1000.0,
        p95_latency_threshold: float = 60.0,
        consecutive_429_threshold: int = 3,
    ) -> None:
        super().__init__(name, config)
        self._token_rate_threshold = token_rate_threshold
        self._p95_latency_threshold = p95_latency_threshold
        self._consecutive_429_threshold = consecutive_429_threshold
        self._recent_latencies: list[float] = []
        self._max_latencies = 100
        self._recent_tokens: list[tuple[float, int]] = []
        self._consecutive_429_count = 0
        self._recent_responses: list[str] = []
        self._max_responses = 10
        self._semantic_failure_count = 0

    async def call_llm(
        self,
        func: Callable[..., Awaitable[T]],
        *args,
        **kwargs,
    ) -> T:
        """Call an LLM with circuit breaker + semantic monitoring."""
        self._stats.total_calls += 1

        if not await self._can_attempt():
            self._stats.rejected_calls += 1
            raise CircuitBreakerOpenError(self.name, self.get_retry_after())

        start = time.monotonic()
        try:
            result = await func(*args, **kwargs)

            tokens = self._extract_tokens(result)
            latency = time.monotonic() - start

            self._record_latency(latency)
            self._record_tokens(time.monotonic(), tokens)
            self._record_response(self._extract_content(result))

            sem_bad = self._check_semantic_degradation()
            if sem_bad:
                self._semantic_failure_count += 1
                if self._semantic_failure_count >= 3:
                    await self._record_failure()
                    raise ValueError(
                        f"LLM semantic failure count {self._semantic_failure_count}"
                    ) from None
            else:
                self._semantic_failure_count = max(0, self._semantic_failure_count - 1)

            rate_bad = self._check_rate_signals(latency)
            if rate_bad:
                await self._record_failure()
                raise ValueError(f"LLM rate/latency signal: rate_bad={rate_bad}")

            await self._record_success()
            return result  # type: ignore[no-any-return]

        except Exception as e:  # broad catch intentional
            if self._is_429(e):
                self._consecutive_429_count += 1
                if self._consecutive_429_count >= self._consecutive_429_threshold:
                    await self._record_failure()
                    raise CircuitBreakerOpenError(
                        self.name,
                        self.get_retry_after(),
                    ) from e
            else:
                self._consecutive_429_count = 0
            await self._record_failure()
            raise

    def _extract_tokens(self, result: Any) -> int:
        if isinstance(result, dict):
            value = result.get("tokens", result.get("total_tokens", 0))
            return int(value) if value is not None else 0
        raw = getattr(result, "total_tokens", 0)
        return int(raw) if raw is not None else 0

    def _extract_content(self, result: Any) -> str:
        if isinstance(result, dict):
            value = result.get("content", result.get("text", ""))
            return str(value) if value is not None else ""
        return str(result) if result is not None else ""

    def _record_latency(self, latency: float) -> None:
        self._recent_latencies.append(latency)
        if len(self._recent_latencies) > self._max_latencies:
            self._recent_latencies.pop(0)

    def _record_tokens(self, ts: float, count: int) -> None:
        self._recent_tokens.append((ts, count))
        cutoff = ts - 60.0
        self._recent_tokens = [(t, c) for t, c in self._recent_tokens if t > cutoff]

    def _record_response(self, content: str) -> None:
        self._recent_responses.append(content)
        if len(self._recent_responses) > self._max_responses:
            self._recent_responses.pop(0)

    def _check_rate_signals(self, latency: float) -> bool:
        if not self._recent_latencies:
            return False
        sorted_lat = sorted(self._recent_latencies)
        p95_idx = int(len(sorted_lat) * 0.95)
        p95 = sorted_lat[min(p95_idx, len(sorted_lat) - 1)]
        latency_bad = p95 > self._p95_latency_threshold

        if not self._recent_tokens:
            return latency_bad
        total_toks = sum(c for _, c in self._recent_tokens)
        span = max(0.1, time.monotonic() - self._recent_tokens[0][0])
        rate = total_toks / span
        rate_bad = rate > self._token_rate_threshold
        return latency_bad or rate_bad

    def _check_semantic_degradation(self) -> bool:
        if len(self._recent_responses) < 3:
            return False
        recent = self._recent_responses[-3:]
        empty = any(not r.strip() for r in recent)
        repeats = len(set(recent)) == 1 and bool(recent[0].strip())
        return empty or repeats

    def _is_429(self, error: Exception) -> bool:
        msg = str(error)
        return "429" in msg or "rate limit" in msg.lower() or "ratelimit" in msg.lower()

    def get_health_report(self) -> dict:
        base = super().get_health_report()
        base["llm_signals"] = {
            "p95_latency": self._calculate_p95(),
            "token_rate": self._calculate_token_rate(),
            "consecutive_429": self._consecutive_429_count,
            "recent_responses": len(self._recent_responses),
            "semantic_failures": self._semantic_failure_count,
            "thresholds": {
                "latency": self._p95_latency_threshold,
                "token_rate": self._token_rate_threshold,
                "consecutive_429": self._consecutive_429_threshold,
            },
        }
        return base

    def _calculate_p95(self) -> float:
        if not self._recent_latencies:
            return 0.0
        sorted_lat = sorted(self._recent_latencies)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    def _calculate_token_rate(self) -> float:
        if not self._recent_tokens:
            return 0.0
        total = sum(c for _, c in self._recent_tokens)
        span = max(0.1, time.monotonic() - self._recent_tokens[0][0])
        return total / span


async def get_llm_circuit_breaker(
    name: str,
    config: CircuitBreakerConfig | None = None,
) -> LLMCircuitBreaker:
    """Get or create an LLM-specific circuit breaker."""
    global _circuits
    async with _get_circuits_lock():
        with _circuits_sync_lock:
            if name not in _circuits:
                _evict_oldest_circuit()
                _circuits[name] = LLMCircuitBreaker(name, config)
            return _circuits[name]  # type: ignore[return-value]


if __name__ == "__main__":
    # Demo
    async def demo():
        cb = CircuitBreaker(
            "test",
            CircuitBreakerConfig(
                failure_threshold=3,
                success_threshold=2,
                timeout_seconds=2.0,
            ),
        )

        logger.info(f"Initial state: {cb.state.value}")

        # Simulate failures
        for i in range(5):
            try:
                await cb.call(lambda: 1 / 0)  # Will raise
            except CircuitBreakerOpenError as e:
                logger.info(f"Circuit open: {e}")
                break
            except ZeroDivisionError:
                await cb._record_failure()
                logger.info(f"Failure {i + 1}: Circuit now {cb.state.value}")

        # Wait for timeout
        logger.info("Waiting for timeout...")
        await asyncio.sleep(3)

        # Check if circuit recovered
        health = cb.get_health_report()
        logger.info(f"Health: {health}")

    asyncio.run(demo())
