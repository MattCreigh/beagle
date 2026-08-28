"""Dynamic Goose Pool Concurrency — CPU-adaptive worker scaling.

Replaces static max_workers with dynamic adjustment based on real-time
CPU utilization via psutil. Scales worker count between min and max
based on CPU pressure.

Config via config.toml [hardware]:
  dynamic_concurrency = true
  concurrency_min = 2
  concurrency_max = 6
  cpu_high_threshold = 80
  cpu_low_threshold = 30

Usage:
    from beagle.core.dynamic_pool import DynamicConcurrency

    dc = DynamicConcurrency(min_workers=2, max_workers=6)
    workers = dc.get_optimal_workers()  # Returns 2-6 based on CPU load
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from beagle.config._config_path import find_config_toml

logger = logging.getLogger("Beagle.dynamic_pool")

_PSUTIL_AVAILABLE: bool = False
try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    logger.debug("[DynamicConcurrency] psutil not installed — using static concurrency")


@dataclass
class ConcurrencyStats:
    """Statistics for dynamic concurrency decisions."""

    current_workers: int = 4
    cpu_percent: float = 0.0
    last_adjustment: float = 0.0
    adjustments_count: int = 0


class DynamicConcurrency:
    """CPU-adaptive worker count adjustment.

    Monitors CPU utilization and adjusts the number of concurrent
    goose workers within configured bounds. High CPU → fewer workers.
    Low CPU → more workers.

    Falls back to static `min(cpu_count, max_workers)` when psutil
    is unavailable.
    """

    def __init__(
        self,
        min_workers: int = 2,
        max_workers: int = 6,
        cpu_high_threshold: float = 80.0,
        cpu_low_threshold: float = 30.0,
        cooldown_seconds: float = 30.0,
    ) -> None:
        """Initialize dynamic concurrency controller.

        Args:
            min_workers: Minimum worker count (floor).
            max_workers: Maximum worker count (ceiling).
            cpu_high_threshold: CPU% above which we scale down.
            cpu_low_threshold: CPU% below which we scale up.
            cooldown_seconds: Minimum seconds between adjustments.

        """
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.cpu_high_threshold = cpu_high_threshold
        self.cpu_low_threshold = cpu_low_threshold
        self.cooldown_seconds = cooldown_seconds
        self._current_workers = min_workers
        self._last_adjustment = 0.0
        self._adjustments_count = 0

    def get_optimal_workers(self) -> int:
        """Get the optimal number of workers based on current CPU load.

        Returns:
            Recommended worker count (between min_workers and max_workers).

        """
        if not _PSUTIL_AVAILABLE:
            # Fallback: use cpu_count-based static value
            import os

            return min(os.cpu_count() or 4, self.max_workers)

        now = time.time()
        # Respect cooldown period
        if now - self._last_adjustment < self.cooldown_seconds:
            return self._current_workers

        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            self._last_adjustment = now

            if cpu_percent > self.cpu_high_threshold:
                # Scale DOWN — CPU is under pressure
                new_workers = max(self.min_workers, self._current_workers - 1)
                if new_workers != self._current_workers:
                    logger.info(
                        f"[DynamicConcurrency] CPU {cpu_percent:.0f}% "
                        f"> {self.cpu_high_threshold:.0f}% → scaling down: "
                        f"{self._current_workers} → {new_workers} workers"
                    )
                    self._current_workers = new_workers
                    self._adjustments_count += 1

            elif cpu_percent < self.cpu_low_threshold:
                # Scale UP — CPU has headroom
                new_workers = min(self.max_workers, self._current_workers + 1)
                if new_workers != self._current_workers:
                    logger.info(
                        f"[DynamicConcurrency] CPU {cpu_percent:.0f}% "
                        f"< {self.cpu_low_threshold:.0f}% → scaling up: "
                        f"{self._current_workers} → {new_workers} workers"
                    )
                    self._current_workers = new_workers
                    self._adjustments_count += 1

            return self._current_workers

        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"[DynamicConcurrency] CPU check failed: {e}")
            return self._current_workers

    def force_workers(self, count: int) -> None:
        """Force set the worker count (overrides dynamic adjustment).

        Args:
            count: Number of workers to use.

        """
        self._current_workers = max(self.min_workers, min(self.max_workers, count))
        self._last_adjustment = time.time()
        logger.debug(f"[DynamicConcurrency] Force-set workers: {self._current_workers}")

    @property
    def stats(self) -> ConcurrencyStats:
        """Get current concurrency statistics."""
        cpu = 0.0
        if _PSUTIL_AVAILABLE:
            with contextlib.suppress(Exception):
                cpu = psutil.cpu_percent(interval=0.0)
        return ConcurrencyStats(
            current_workers=self._current_workers,
            cpu_percent=cpu,
            last_adjustment=self._last_adjustment,
            adjustments_count=self._adjustments_count,
        )


def is_dynamic_concurrency_enabled() -> bool:
    """Check if dynamic concurrency is enabled via config.toml."""
    try:
        config_path = find_config_toml()
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            return data.get("hardware", {}).get("dynamic_concurrency", True)
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError) as exc:
        logger.warning(
            "Cannot read [hardware].dynamic_concurrency from config.toml (%s); "
            "leaving dynamic concurrency enabled.",
            exc,
        )
    return True


def get_dynamic_concurrency() -> DynamicConcurrency:
    """Create a DynamicConcurrency instance from config.toml settings."""
    min_w = 2
    max_w = 6
    cpu_high = 80.0
    cpu_low = 30.0
    try:
        config_path = find_config_toml()
        if config_path.exists():
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            hw = data.get("hardware", {})
            min_w = hw.get("concurrency_min", 2)
            max_w = hw.get("concurrency_max", 6)
            cpu_high = hw.get("cpu_high_threshold", 80.0)
            cpu_low = hw.get("cpu_low_threshold", 30.0)
    except (OSError, tomllib.TOMLDecodeError, TypeError, AttributeError) as exc:
        logger.warning(
            "Cannot read the [hardware] concurrency thresholds from config.toml (%s); "
            "using the built-in defaults (min=%s, max=%s, cpu_high=%s, cpu_low=%s).",
            exc,
            min_w,
            max_w,
            cpu_high,
            cpu_low,
        )
    return DynamicConcurrency(
        min_workers=min_w,
        max_workers=max_w,
        cpu_high_threshold=cpu_high,
        cpu_low_threshold=cpu_low,
    )


# ── Phase 2 (fault-recovery hardening): LLM backpressure + circuit breaker ──


class LLMBackpressure:
    """Wrap LLM API calls with a shared semaphore + per-provider circuit breaker.

    Provides backpressure (a global concurrency semaphore) and per-provider
    fault tolerance (a circuit breaker that trips on consecutive 429/504
    responses and routes the failure to the SQLite dead-letter queue).

    This is best-effort hardening: if the circuit-breaker layer is unavailable
    the call still runs under the semaphore, preserving existing behaviour.
    """

    def __init__(
        self,
        max_concurrency: int = 4,
        circuit_config: Any | None = None,
    ) -> None:
        """Initialise the backpressure wrapper.

        Args:
            max_concurrency: Max simultaneous LLM calls (backpressure ceiling).
            circuit_config: Optional ``CircuitBreakerConfig`` for the breaker.
        """
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self.circuit_config = circuit_config
        # Cache of lazily-created per-provider circuit breakers.
        self._circuits: dict[str, Any] = {}
        self._circuits_lock = asyncio.Lock()

    async def _get_circuit(self, provider: str) -> Any | None:
        """Get (or create) the circuit breaker for a provider, best-effort."""
        if provider in self._circuits:
            return self._circuits[provider]
        try:
            from beagle.utils.circuit_breaker import (
                CircuitBreakerConfig,
                get_llm_circuit_breaker,
            )

            cfg = self.circuit_config or CircuitBreakerConfig()
            async with self._circuits_lock:
                if provider not in self._circuits:
                    # Scoped name carries workflow/dag context for the DLQ.
                    self._circuits[provider] = await get_llm_circuit_breaker(
                        f"llm-api:{provider}",
                        cfg,
                    )
            return self._circuits[provider]
        except Exception as exc:  # broad catch: circuit layer is best-effort
            logger.debug("[LLMBackpressure] circuit breaker unavailable (%s)", exc)
            return None

    async def call(
        self,
        provider: str,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute an LLM call under backpressure + circuit breaker.

        Args:
            provider: Provider/model key (e.g. ``"deepseek-v4-flash"``) used
                to scope the per-provider circuit breaker and DLQ routing.
            func: The async LLM-calling coroutine.
            *args, **kwargs: Passed through to ``func``.

        Returns:
            The result of ``func``.

        Raises:
            Exception: Whatever ``func`` (or the circuit breaker) raises.
        """
        circuit = await self._get_circuit(provider)
        async with self._semaphore:
            if circuit is None:
                # No circuit layer available — run directly under backpressure.
                return await func(*args, **kwargs)
            if hasattr(circuit, "call_llm"):
                return await circuit.call_llm(func, *args, **kwargs)
            return await circuit.call(func, *args, **kwargs)


__all__ = [
    "ConcurrencyStats",
    "DynamicConcurrency",
    "LLMBackpressure",
    "get_dynamic_concurrency",
    "is_dynamic_concurrency_enabled",
]
