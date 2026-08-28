import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from random import SystemRandom
from typing import Any

from .base import RateLimiter
from .config import RateLimitConfig

logger = logging.getLogger("Beagle.utils.rate_limiter.workflow")

# <invariant>
# Backoff jitter uses SystemRandom, not the `random` module's Mersenne
# Twister. Jitter is not a security decision, so the entropy source does not
# have to be cryptographic — but a predictable module-global sequence lets a
# caller anticipate every retry instant across the process, and SystemRandom
# costs nothing at backoff frequency. Using it removes the finding rather than
# suppressing it.
# </invariant>
_JITTER_RNG = SystemRandom()

# v13.20.13 (R6.3): the four backoff knobs are now config-driven via
# the `[rate_limiter]` section in config.toml (SSOT per doctrine
# "config.toml is the SSOT for config"). The module-level constants
# remain as the in-code default-fallback when the config section is
# missing (e.g. unit tests that construct a `WorkflowRateLimiter`
# before `get_config()` is initialised). On the deployed edge target
# the config.toml values are read once at import time; changing them
# requires a process restart (intentional — the constants are
# process-global and mutating them at runtime would race with
# concurrent backoff calculations).
#
# To tune the backoff, edit config.toml:
#   [rate_limiter]
#   initial_backoff = 1.0   # seconds
#   max_backoff = 120.0     # seconds (2 minutes)
#   backoff_multiplier = 2.0
#   jitter_factor = 0.25    # ±25% jitter to prevent thundering herd
try:
    from beagle.config.config import get_config

    _rl_cfg = get_config().rate_limit
    _INITIAL_BACKOFF = float(getattr(_rl_cfg, "initial_backoff", 1.0))
    _MAX_BACKOFF = float(getattr(_rl_cfg, "max_backoff", 120.0))
    _BACKOFF_MULTIPLIER = float(getattr(_rl_cfg, "backoff_multiplier", 2.0))
    _JITTER_FACTOR = float(getattr(_rl_cfg, "jitter_factor", 0.25))
except (ImportError, AttributeError, ValueError, TypeError):
    # Config not yet initialised or keys missing — fall back to the
    # historical in-code defaults. This path is hit by unit tests
    # and by any code that imports WorkflowRateLimiter before
    # `get_config()` has been called.
    _INITIAL_BACKOFF = 1.0
    _MAX_BACKOFF = 120.0
    _BACKOFF_MULTIPLIER = 2.0
    _JITTER_FACTOR = 0.25


class WorkflowRateLimiter:
    """Rate limiter for managing multiple workflows and models."""

    def __init__(
        self,
        default_requests_per_second: float = 1.0,
        default_burst_size: int = 10,
        workflow_configs: dict[str, RateLimitConfig] | None = None,
        model_configs: dict[str, RateLimitConfig] | None = None,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: float = 60.0,
    ) -> None:
        """Initialize workflow rate limiter."""
        self.default_config = RateLimitConfig(
            requests_per_second=default_requests_per_second,
            burst_size=default_burst_size,
        )
        self.workflow_configs = workflow_configs or {}
        self.model_configs = model_configs or {}
        self._workflow_limiters: dict[str, RateLimiter] = {}
        self._model_limiters: dict[str, RateLimiter] = {}
        self._failure_counts: dict[str, int] = {}
        self._success_counts: dict[str, int] = {}
        self._last_failure_time: dict[str, float] = {}
        self._backoff_multiplier: dict[str, float] = {}  # Per-entity backoff
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_timeout = circuit_breaker_timeout  # seconds
        self._circuit_breaker_state: dict[str, str] = {}  # Per-entity circuit state
        self._last_circuit_change: dict[str, float] = {}  # When circuit last changed
        self._lock = threading.Lock()
        self._cleanup_interval = 300.0  # 5 minutes
        self._last_cleanup = time.monotonic()
        self._max_tracked_entities = 10_000
        self._last_access: dict[str, float] = {}  # v0.3.0: LRU tracking

    def _calculate_backoff(self, entity_id: str) -> float:
        """Calculate exponential backoff with jitter for an entity.

        Args:
            entity_id: Workflow or model identifier

        Returns:
            Backoff time in seconds

        """
        self._last_access[entity_id] = time.monotonic()  # v0.3.0: LRU touch
        multiplier = self._backoff_multiplier.get(entity_id, _INITIAL_BACKOFF)
        # Apply jitter: ±25% randomization
        jitter = multiplier * _JITTER_FACTOR * (2 * _JITTER_RNG.random() - 1)
        backoff = min(multiplier + jitter, _MAX_BACKOFF)
        logger.info(
            f"[{entity_id}] Backoff calculated: {backoff:.2f}s (multiplier={multiplier:.2f})"
        )
        return backoff

    def _increment_backoff(self, entity_id: str) -> None:
        """Increase backoff multiplier for an entity after a failure."""
        current = self._backoff_multiplier.get(entity_id, _INITIAL_BACKOFF)
        new_multiplier = min(current * _BACKOFF_MULTIPLIER, _MAX_BACKOFF)
        self._backoff_multiplier[entity_id] = new_multiplier
        logger.debug(f"[{entity_id}] Backoff increased to {new_multiplier:.2f}s")

    def _reset_backoff(self, entity_id: str) -> None:
        """Reset backoff multiplier for an entity after success."""
        if entity_id in self._backoff_multiplier:
            self._backoff_multiplier[entity_id] = _INITIAL_BACKOFF

    def trigger_429_backoff(self, entity_id: str = "default") -> float:
        """Trigger exponential backoff for rate limit responses (429).

        When a 429 is received, this implements exponential backoff with
        jitter to prevent thundering herd and give the API time to recover.

        Args:
            entity_id: Optional workflow or model identifier for tracking

        Returns:
            Recommended sleep time in seconds before retry

        """
        backoff = self._calculate_backoff(entity_id)
        self._increment_backoff(entity_id)

        # Record as failure for circuit breaker tracking
        self.record_failure(entity_id)

        logger.warning(f"[{entity_id}] 429 Rate Limit hit - backing off for {backoff:.2f}s")
        return backoff

    def record_failure(self, entity_id: str = "default") -> None:
        """Record a failure for circuit breaker logic.

        Tracks consecutive failures and adjusts backoff accordingly.

        Args:
            entity_id: Workflow or model identifier

        """
        with self._lock:
            self._failure_counts[entity_id] = self._failure_counts.get(entity_id, 0) + 1
            self._last_failure_time[entity_id] = time.monotonic()
            self._success_counts.pop(entity_id, None)  # Reset success streak

            # Increment backoff on each failure
            self._increment_backoff(entity_id)

            logger.debug(
                f"[{entity_id}] Failure recorded: "
                f"{self._failure_counts[entity_id]} consecutive, "
                f"current backoff: {self._backoff_multiplier.get(entity_id, _INITIAL_BACKOFF):.2f}s"
            )

            # Check if circuit breaker should trip
            if self._failure_counts[entity_id] >= self._circuit_breaker_threshold:
                self._trip_circuit_breaker(entity_id)

    def record_success(self, entity_id: str = "default") -> None:
        """Record a success for circuit breaker logic.

        Resets failure counts and backoff on successful requests.

        Args:
            entity_id: Workflow or model identifier

        """
        with self._lock:
            self._success_counts[entity_id] = self._success_counts.get(entity_id, 0) + 1

            # Reset failure tracking on success
            if entity_id in self._failure_counts:
                self._failure_counts[entity_id] = 0

            # Reset backoff to initial on success
            self._reset_backoff(entity_id)

            # Check if circuit breaker should close (success after failures)
            if self._success_counts[entity_id] >= 3:
                self._close_circuit_breaker(entity_id)
                self._success_counts[entity_id] = 0

            logger.debug(
                f"[{entity_id}] Success recorded, backoff reset to {_INITIAL_BACKOFF:.2f}s"
            )

    def _trip_circuit_breaker(self, entity_id: str) -> None:
        """Mark entity as in circuit-breaker open state."""
        self._circuit_breaker_state[entity_id] = "open"
        self._last_circuit_change[entity_id] = time.monotonic()
        logger.warning(
            f"[{entity_id}] Circuit breaker TRIPPED after "
            f"{self._failure_counts[entity_id]} consecutive failures"
        )

    def _try_half_open(self, entity_id: str) -> bool:
        """Attempt to transition to half-open state for recovery testing.

        Args:
            entity_id: Workflow or model identifier

        Returns:
            True if transition to half-open was successful

        """
        if self._circuit_breaker_state.get(entity_id) != "open":
            return False

        timeout_elapsed = (
            time.monotonic() - self._last_circuit_change.get(entity_id, 0)
        ) >= self._circuit_breaker_timeout

        if timeout_elapsed:
            self._circuit_breaker_state[entity_id] = "half_open"
            self._last_circuit_change[entity_id] = time.monotonic()
            logger.info(f"[{entity_id}] Circuit breaker HALF-OPEN - testing recovery")
            return True
        return False

    def _close_circuit_breaker(self, entity_id: str) -> None:
        """Close circuit breaker after successful recovery."""
        self._circuit_breaker_state[entity_id] = "closed"
        self._last_circuit_change[entity_id] = time.monotonic()
        self._failure_counts[entity_id] = 0
        self._backoff_multiplier[entity_id] = _INITIAL_BACKOFF
        logger.info(f"[{entity_id}] Circuit breaker CLOSED - recovery successful")

    def _reset_circuit(self, entity_id: str | None = None) -> None:
        """Reset circuit breaker state.

        Args:
            entity_id: Optional specific entity to reset. If None, resets all.

        """
        with self._lock:
            if entity_id:
                if entity_id in self._circuit_breaker_state:
                    self._circuit_breaker_state[entity_id] = "closed"
                    self._failure_counts[entity_id] = 0
                    self._backoff_multiplier[entity_id] = _INITIAL_BACKOFF
            else:
                # Reset all
                for eid in list(self._circuit_breaker_state.keys()):
                    self._circuit_breaker_state[eid] = "closed"
                    self._failure_counts[eid] = 0
                    self._backoff_multiplier[eid] = _INITIAL_BACKOFF

    def circuit_state(self, entity_id: str = "default") -> str:
        """Get current circuit breaker state for an entity.

        Args:
            entity_id: Workflow or model identifier

        Returns:
            Circuit state: "closed", "open", or "half_open"

        """
        return self._circuit_breaker_state.get(entity_id, "closed")

    def is_circuit_open(self, entity_id: str = "default") -> bool:
        """Check if circuit breaker is open for an entity.

        Attempts automatic transition to half-open state after timeout.

        Args:
            entity_id: Workflow or model identifier

        Returns:
            True if circuit is open (fast-failing)

        """
        if self.circuit_state(entity_id) == "open":
            self._try_half_open(entity_id)
        return self.circuit_state(entity_id) == "open"

    def get_backoff_info(self, entity_id: str = "default") -> dict[str, Any]:
        """Get backoff information for an entity.

        Args:
            entity_id: Workflow or model identifier

        Returns:
            Dictionary with backoff state

        """
        return {
            "entity_id": entity_id,
            "current_backoff": self._backoff_multiplier.get(entity_id, _INITIAL_BACKOFF),
            "consecutive_failures": self._failure_counts.get(entity_id, 0),
            "circuit_state": self.circuit_state(entity_id),
            "last_failure_time": self._last_failure_time.get(entity_id, 0),
        }

    def _evict_lru(self, limiters: dict) -> None:
        """Evict the least-recently-accessed entity from a limiter dict.

        v0.3.0: Uses LRU instead of FIFO eviction.
        """
        if not limiters:
            return
        # Find LRU entity
        lru_key = min(
            limiters.keys(),
            key=lambda k: self._last_access.get(k, 0.0),
        )
        del limiters[lru_key]
        self._last_access.pop(lru_key, None)

    def get_workflow_limiter(self, workflow_id: str) -> RateLimiter:
        """Get or create rate limiter for a specific workflow."""
        with self._lock:
            self._last_access[workflow_id] = time.monotonic()
            if workflow_id not in self._workflow_limiters:
                if len(self._workflow_limiters) >= self._max_tracked_entities:
                    self._evict_lru(self._workflow_limiters)
                config = self.workflow_configs.get(workflow_id, self.default_config)
                self._workflow_limiters[workflow_id] = RateLimiter(config)
            return self._workflow_limiters[workflow_id]

    def get_model_limiter(self, model: str) -> RateLimiter:
        """Get or create rate limiter for a specific model."""
        with self._lock:
            self._last_access[model] = time.monotonic()
            if model not in self._model_limiters:
                if len(self._model_limiters) >= self._max_tracked_entities:
                    self._evict_lru(self._model_limiters)
                config = self.model_configs.get(model, self.default_config)
                self._model_limiters[model] = RateLimiter(config)
            return self._model_limiters[model]

    def acquire(
        self,
        estimated_tokens: int = 1000,
        workflow_id: str | None = None,
        model: str | None = None,
        block: bool = True,
    ) -> float:
        """
        Acquire quota for a request with workflow/model-specific limits.

        Args:
            estimated_tokens: Estimated number of tokens for the request
            workflow_id: Optional workflow identifier
            model: Optional model identifier
            block: Whether to block until quota is available

        Returns:
            Time waited in seconds

        Raises:
            ValueError: If both workflow_id and model are provided

        """
        if workflow_id is not None and model is not None:
            raise ValueError("Cannot specify both workflow_id and model")

        limiter = self._get_limiter_for_entity(workflow_id, model)
        return limiter.wait_for_tokens(estimated_tokens, block=block)

    def consume_tokens(
        self, tokens: int, workflow_id: str | None = None, model: str | None = None
    ) -> bool:
        """Consume tokens from the appropriate rate limiter.

        Args:
            tokens: Number of tokens to consume
            workflow_id: Optional workflow identifier
            model: Optional model identifier

        Returns:
            True if tokens were consumed, False otherwise

        """
        limiter = self._get_limiter_for_entity(workflow_id, model)
        return limiter.consume_tokens(tokens)

    def cleanup_workflow(self, workflow_id: str) -> None:
        """Clean up resources for a specific workflow."""
        with self._lock:
            self._workflow_limiters.pop(workflow_id, None)
            self._failure_counts.pop(workflow_id, None)
            self._success_counts.pop(workflow_id, None)
            self._last_failure_time.pop(workflow_id, None)

    def stats(self) -> dict[str, Any]:
        """Get comprehensive statistics for all limiters."""
        with self._lock:
            # Cleanup old entries periodically
            now = time.monotonic()
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_old_entries()
                self._last_cleanup = now

            workflow_stats = {}
            for wid, limiter in self._workflow_limiters.items():
                workflow_stats[wid] = limiter.stats()

            model_stats = {}
            for mid, limiter in self._model_limiters.items():
                model_stats[mid] = limiter.stats()

            return {
                "workflows": workflow_stats,
                "models": model_stats,
                "total_workflows": len(self._workflow_limiters),
                "total_models": len(self._model_limiters),
            }

    def _cleanup_old_entries(self) -> None:
        """Clean up stale entries to prevent memory leaks.

        Removes circuit breaker state for entities with no recent activity.
        """
        now = time.monotonic()
        stale_threshold = self._cleanup_interval * 2  # 10 minutes of inactivity

        # Clean up stale failure/success tracking for inactive entities
        stale_entities = [
            eid
            for eid, last_time in self._last_failure_time.items()
            if now - last_time > stale_threshold
        ]
        for eid in stale_entities:
            # Only clean up if not actively tracked by limiters
            if eid not in self._workflow_limiters and eid not in self._model_limiters:
                self._failure_counts.pop(eid, None)
                self._success_counts.pop(eid, None)
                self._last_failure_time.pop(eid, None)
                self._backoff_multiplier.pop(eid, None)
                self._circuit_breaker_state.pop(eid, None)
                self._last_circuit_change.pop(eid, None)
                logger.debug(f"[{eid}] Cleaned up stale circuit breaker state")

    @asynccontextmanager
    async def acquire_async(
        self,
        estimated_tokens: int = 1000,
        workflow_id: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async context manager for acquiring rate limit tokens.

        Automatically handles waiting and records success/failure.

        Args:
            estimated_tokens: Estimated number of tokens for the request
            workflow_id: Optional workflow identifier
            model: Optional model identifier

        Yields:
            Dictionary with acquisition info including wait_time and entity_id

        Raises:
            RuntimeError: If circuit breaker is open

        """
        entity_id = workflow_id or model or "default"

        # Check circuit breaker before waiting
        if self.is_circuit_open(entity_id):
            last_change = self._last_circuit_change.get(entity_id, 0)
            timeout_remaining = max(
                0, self._circuit_breaker_timeout - (time.monotonic() - last_change)
            )
            raise RuntimeError(
                f"Circuit breaker is open for {entity_id}. Retry in {timeout_remaining:.1f}s"
            )

        try:
            # Get the appropriate limiter
            limiter = self._get_limiter_for_entity(workflow_id, model)

            # Wait for tokens to become available
            wait_time = limiter.bucket.time_until_available(estimated_tokens)
            if wait_time > 0:
                await asyncio.sleep(wait_time)

            # Consume tokens atomically
            if not limiter.consume_tokens(estimated_tokens):
                # Tokens not available after waiting (race condition)
                wait_time = limiter.bucket.time_until_available(estimated_tokens)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                limiter.consume_tokens(estimated_tokens)

            # Re-check circuit breaker after acquisition (may have changed during wait)
            if self.is_circuit_open(entity_id):
                last_change = self._last_circuit_change.get(entity_id, 0)
                timeout_remaining = max(
                    0, self._circuit_breaker_timeout - (time.monotonic() - last_change)
                )
                # Refund tokens if circuit opened during wait
                self.record_failure(entity_id)
                raise RuntimeError(
                    f"Circuit breaker is open for {entity_id}. Retry in {timeout_remaining:.1f}s"
                )

            yield {"wait_time": wait_time, "entity_id": entity_id}

            # Record success on clean exit
            self.record_success(entity_id)

        except Exception:  # broad catch intentional
            # Record failure on exception
            self.record_failure(entity_id)
            raise

    def _get_limiter_for_entity(
        self, workflow_id: str | None = None, model: str | None = None
    ) -> RateLimiter:
        """Get the appropriate rate limiter for the given entity.

        Args:
            workflow_id: Optional workflow identifier
            model: Optional model identifier

        Returns:
            Appropriate RateLimiter instance

        """
        if workflow_id:
            return self.get_workflow_limiter(workflow_id)
        elif model:
            return self.get_model_limiter(model)
        else:
            return RateLimiter(self.default_config)


def _get_rate_limiter_lock() -> asyncio.Lock:
    """Get or create the global rate limiter lock."""
    if not hasattr(_get_rate_limiter_lock, "_lock"):
        setattr(_get_rate_limiter_lock, "_lock", asyncio.Lock())
    return getattr(_get_rate_limiter_lock, "_lock")


def get_rate_limiter_async() -> WorkflowRateLimiter:
    """Get the singleton async rate limiter instance."""
    if not hasattr(get_rate_limiter_async, "_instance"):
        setattr(get_rate_limiter_async, "_instance", WorkflowRateLimiter())
    return getattr(get_rate_limiter_async, "_instance")


def get_rate_limiter() -> WorkflowRateLimiter:
    """Get the singleton rate limiter instance."""
    if not hasattr(get_rate_limiter, "_instance"):
        setattr(get_rate_limiter, "_instance", WorkflowRateLimiter())
    return getattr(get_rate_limiter, "_instance")


def reset_rate_limiter_async(
    workflow_configs: dict[str, RateLimitConfig] | None = None,
    model_configs: dict[str, RateLimitConfig] | None = None,
    default_requests_per_second: float = 1.0,
    default_burst_size: int = 10,
) -> WorkflowRateLimiter:
    """Reset the singleton async rate limiter instance."""
    if hasattr(get_rate_limiter_async, "_instance"):
        del get_rate_limiter_async._instance
    instance = WorkflowRateLimiter(
        default_requests_per_second=default_requests_per_second,
        default_burst_size=default_burst_size,
        workflow_configs=workflow_configs,
        model_configs=model_configs,
    )
    get_rate_limiter_async._instance = instance  # type: ignore[attr-defined]
    return instance


def reset_rate_limiter(
    workflow_configs: dict[str, RateLimitConfig] | None = None,
    model_configs: dict[str, RateLimitConfig] | None = None,
    default_requests_per_second: float = 1.0,
    default_burst_size: int = 10,
) -> WorkflowRateLimiter:
    """Reset the singleton rate limiter instance."""
    if hasattr(get_rate_limiter, "_instance"):
        del get_rate_limiter._instance
    instance = WorkflowRateLimiter(
        default_requests_per_second=default_requests_per_second,
        default_burst_size=default_burst_size,
        workflow_configs=workflow_configs,
        model_configs=model_configs,
    )
    get_rate_limiter._instance = instance  # type: ignore[attr-defined]
    return instance


def demo() -> None:
    """Demonstrate rate limiter functionality."""
    logger.info("Rate Limiter Demo")
    logger.info("=" * 50)

    # Basic rate limiter demo
    logger.info("\n1. Basic Rate Limiter:")
    limiter = RateLimiter(RateLimitConfig(requests_per_second=2.0, burst_size=5))
    logger.info(f"   Initial tokens: {limiter.bucket.available()}")
    logger.info(f"   Request allowed: {limiter.consume_request()}")
    logger.info(f"   Tokens remaining: {limiter.bucket.available()}")

    # Workflow rate limiter demo
    logger.info("\n2. Workflow Rate Limiter:")
    workflow_limiter = WorkflowRateLimiter(default_requests_per_second=1.0, default_burst_size=10)

    # Add specific workflow config
    workflow_limiter.workflow_configs["test_workflow"] = RateLimitConfig(
        requests_per_second=2.0, burst_size=20
    )

    # Test acquisition
    wait_time = workflow_limiter.acquire(100, workflow_id="test_workflow")
    logger.info(f"   Wait time for 100 tokens: {wait_time:.3f}s")
    logger.info(
        f"   Available tokens: "
        f"{workflow_limiter.get_workflow_limiter('test_workflow').bucket.available():.1f}"
    )

    # Stats demo
    logger.info("\n3. Statistics:")
    stats = workflow_limiter.stats()
    logger.info(f"   Total workflows: {stats['total_workflows']}")
    logger.info(f"   Total models: {stats['total_models']}")


if __name__ == "__main__":
    demo()
