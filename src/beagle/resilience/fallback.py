"""Fallback chain for model and resource degradation.

Primary -> Secondary -> Tertiary -> Local -> Cached

Each level has its own circuit breaker.
The chain MUST terminate at a resource we control (Cached).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from beagle.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
)

logger = logging.getLogger("Beagle.resilience.fallback")


class FallbackLevel(StrEnum):
    """Levels in the fallback chain."""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"
    LOCAL = "local"
    CACHED = "cached"
    FAILED = "failed"


@dataclass(frozen=True)
class FallbackResult:
    """Result from a fallback chain attempt."""

    success: bool
    level: FallbackLevel
    content: str = ""
    error: str = ""
    metadata: dict[str, Any] | None = None

    @property
    def used_fallback(self) -> bool:
        return self.level != FallbackLevel.PRIMARY


class FallbackChain:
    """Chain of fallbacks for model/resource calls.

    Each level attempts the call with its own circuit breaker.
    If a level fails or is OPEN, the next level is tried.
    The chain always terminates at CACHED, which must work.

    Usage:
        chain = FallbackChain(
            primary=call_cloud_llm,
            secondary=call_cloud_llm_v2,
            tertiary=call_backup_provider,
            local=call_ollama,
            cached=call_cache,
        )
        result = await chain.execute("prompt here")
    """

    def __init__(
        self,
        primary: Callable[..., str],
        secondary: Callable[..., str] | None = None,
        tertiary: Callable[..., str] | None = None,
        local: Callable[..., str] | None = None,
        cached: Callable[..., str] | None = None,
        circuit_config: CircuitBreakerConfig | None = None,
    ) -> None:
        self._handlers: dict[FallbackLevel, Callable[..., Any]] = {
            FallbackLevel.PRIMARY: primary,
        }
        if secondary:
            self._handlers[FallbackLevel.SECONDARY] = secondary
        if tertiary:
            self._handlers[FallbackLevel.TERTIARY] = tertiary
        if local:
            self._handlers[FallbackLevel.LOCAL] = local
        if cached:
            self._handlers[FallbackLevel.CACHED] = cached

        # Ensure at least cached or local exists as termination
        if FallbackLevel.CACHED not in self._handlers and FallbackLevel.LOCAL not in self._handlers:
            raise ValueError("FallbackChain requires at least a 'cached' or 'local' terminal")

        cfg = circuit_config or CircuitBreakerConfig(
            failure_threshold=3,
            success_threshold=2,
            timeout_seconds=30.0,
        )
        self._circuits: dict[FallbackLevel, CircuitBreaker] = {}
        for level in self._handlers:
            self._circuits[level] = CircuitBreaker(
                f"fallback_{level.value}",
                CircuitBreakerConfig(
                    failure_threshold=cfg.failure_threshold,
                    success_threshold=cfg.success_threshold,
                    timeout_seconds=cfg.timeout_seconds,
                ),
            )

        self._attempt_order: list[FallbackLevel] = [
            FallbackLevel.PRIMARY,
            FallbackLevel.SECONDARY,
            FallbackLevel.TERTIARY,
            FallbackLevel.LOCAL,
            FallbackLevel.CACHED,
        ]
        self._attempt_order = [level for level in self._attempt_order if level in self._handlers]

    # ── Execution ────────────────────────────────────────────────────────────

    async def execute(self, *args: Any, **kwargs: Any) -> FallbackResult:
        """Execute the fallback chain and return the first success.

        Args:
            *args: Passed to each handler attempt.
            **kwargs: Passed to each handler attempt.

        Returns:
            FallbackResult with success status, level used, and content/error.

        """
        last_error = "No fallback available"
        for level in self._attempt_order:
            circuit = self._circuits[level]
            try:
                # Check circuit state
                if not await circuit._can_attempt():
                    last_error = f"Circuit for {level.value} is OPEN"
                    continue

                handler = self._handlers[level]
                import asyncio

                if asyncio.iscoroutinefunction(handler):
                    content = await handler(*args, **kwargs)
                else:
                    content = handler(*args, **kwargs)

                await circuit._record_success()
                return FallbackResult(
                    success=True,
                    level=level,
                    content=str(content) if content is not None else "",
                    metadata={"fallback_level": level.value},
                )
            except CircuitBreakerOpenError:
                last_error = f"Circuit for {level.value} is OPEN"
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                await circuit._record_failure()
                last_error = f"{level.value} failed: {e}"
                logger.warning(last_error)

        # All fallbacks exhausted — chain MUST terminate, but everything failed
        logger.error(f"Fallback chain exhausted: {last_error}")
        return FallbackResult(
            success=False,
            level=FallbackLevel.FAILED,
            error=last_error,
            content="[FALLBACK_EXHAUSTED]",
        )

    def get_health(self) -> dict[str, Any]:
        """Get health report for all fallback levels."""
        return {
            level.value: {
                "state": cb.state.value,
                "stats": {
                    "total_calls": cb.stats.total_calls,
                    "successful_calls": cb.stats.successful_calls,
                    "failed_calls": cb.stats.failed_calls,
                    "rejected_calls": cb.stats.rejected_calls,
                },
            }
            for level, cb in self._circuits.items()
        }
