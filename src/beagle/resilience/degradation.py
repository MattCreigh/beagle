"""System degradation manager for Beagle.

Implements multi-level graceful degradation:
- Normal: Full functionality
- Degraded-1: Secondary model fallback
- Degraded-2: Local model fallback
- Degraded-3: Cached/deterministic only, no MCP tools
- Degraded-4: No MCP, queue workflows
- Emergency: Full halt, alert operator
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger("Beagle.resilience.degradation")


class DegradationLevel(StrEnum):
    """Degradation levels from normal to emergency."""

    NORMAL = "normal"
    DEGRADED_1 = "degraded-1"  # secondary model
    DEGRADED_2 = "degraded-2"  # local model
    DEGRADED_3 = "degraded-3"  # cached/deterministic only
    DEGRADED_4 = "degraded-4"  # no MCP, queue workflows
    EMERGENCY = "emergency"  # halt workflows, alert


class TriggerType(StrEnum):
    """Types of triggers that cause degradation changes."""

    CIRCUIT_BREAKER = "circuit_breaker"
    MCP_HEALTH = "mcp_health"
    BUDGET = "budget"
    MANUAL = "manual"
    HEALTH_SCORE = "health_score"


@dataclass
class DegradationState:
    """Snapshot of current degradation state."""

    level: DegradationLevel
    previous_level: DegradationLevel
    reason: str
    triggered_by: TriggerType
    model_preference: str
    enable_mcp: bool
    cache_only: bool
    queue_workflows: bool
    alert_operator: bool
    changed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "previous_level": self.previous_level.value,
            "reason": self.reason,
            "triggered_by": self.triggered_by.value,
            "model_preference": self.model_preference,
            "enable_mcp": self.enable_mcp,
            "cache_only": self.cache_only,
            "queue_workflows": self.queue_workflows,
            "alert_operator": self.alert_operator,
            "changed_at": self.changed_at,
        }


# ── Level configurations ──────────────────────────────────────────────────

_LEVEL_CONFIG: dict[DegradationLevel, dict[str, Any]] = {
    DegradationLevel.NORMAL: {
        "model_preference": "primary",
        "enable_mcp": True,
        "cache_only": False,
        "queue_workflows": False,
        "alert_operator": False,
    },
    DegradationLevel.DEGRADED_1: {
        "model_preference": "secondary",
        "enable_mcp": True,
        "cache_only": False,
        "queue_workflows": False,
        "alert_operator": False,
    },
    DegradationLevel.DEGRADED_2: {
        "model_preference": "local",
        "enable_mcp": True,
        "cache_only": False,
        "queue_workflows": False,
        "alert_operator": True,
    },
    DegradationLevel.DEGRADED_3: {
        "model_preference": "local",
        "enable_mcp": False,
        "cache_only": True,
        "queue_workflows": False,
        "alert_operator": True,
    },
    DegradationLevel.DEGRADED_4: {
        "model_preference": "local",
        "enable_mcp": False,
        "cache_only": True,
        "queue_workflows": True,
        "alert_operator": True,
    },
    DegradationLevel.EMERGENCY: {
        "model_preference": "none",
        "enable_mcp": False,
        "cache_only": False,
        "queue_workflows": True,
        "alert_operator": True,
    },
}


# ── Level transitions ───────────────────────────────────────────────────────

_TRANSITION_UP: dict[DegradationLevel, DegradationLevel] = {
    DegradationLevel.EMERGENCY: DegradationLevel.DEGRADED_4,
    DegradationLevel.DEGRADED_4: DegradationLevel.DEGRADED_3,
    DegradationLevel.DEGRADED_3: DegradationLevel.DEGRADED_2,
    DegradationLevel.DEGRADED_2: DegradationLevel.DEGRADED_1,
    DegradationLevel.DEGRADED_1: DegradationLevel.NORMAL,
    DegradationLevel.NORMAL: DegradationLevel.NORMAL,
}

_TRANSITION_DOWN: dict[DegradationLevel, DegradationLevel] = {
    DegradationLevel.NORMAL: DegradationLevel.DEGRADED_1,
    DegradationLevel.DEGRADED_1: DegradationLevel.DEGRADED_2,
    DegradationLevel.DEGRADED_2: DegradationLevel.DEGRADED_3,
    DegradationLevel.DEGRADED_3: DegradationLevel.DEGRADED_4,
    DegradationLevel.DEGRADED_4: DegradationLevel.EMERGENCY,
    DegradationLevel.EMERGENCY: DegradationLevel.EMERGENCY,
}


class DegradationManager:
    """Manages system degradation levels and transitions.

    Subscribes to circuit breaker, MCP health, and budget events.
    Emits DegradationChanged events on the EventBus.

    Usage:
        mgr = DegradationManager()
        await mgr.start()
        await mgr.check_health()  # periodic
    """

    def __init__(
        self,
        event_bus: Any | None = None,
        recovery_interval_seconds: float = 60.0,
    ) -> None:
        self._level = DegradationLevel.NORMAL
        self._state_history: list[dict[str, Any]] = []
        self._event_bus = event_bus
        self._recovery_interval = recovery_interval_seconds
        self._last_recovery_check = time.monotonic()
        self._lock = asyncio.Lock()
        self._subs: list[Any] = []
        self._running = False

    @property
    def current_level(self) -> DegradationLevel:
        return self._level

    @property
    def current_state(self) -> DegradationState:
        cfg = _LEVEL_CONFIG[self._level]
        return DegradationState(
            level=self._level,
            previous_level=self._level,
            reason="current",
            triggered_by=TriggerType.MANUAL,
            model_preference=cfg["model_preference"],
            enable_mcp=cfg["enable_mcp"],
            cache_only=cfg["cache_only"],
            queue_workflows=cfg["queue_workflows"],
            alert_operator=cfg["alert_operator"],
        )

    # ── Event subscriptions ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to EventBus events that may trigger degradation."""
        if self._running:
            return
        self._running = True
        logger.info(f"DegradationManager started at level {self._level.value}")

        if self._event_bus is not None:
            try:
                sub1 = self._event_bus.subscribe("health.degraded", self._on_health_degraded)
                sub2 = self._event_bus.subscribe("health.critical", self._on_health_critical)
                sub3 = self._event_bus.subscribe("health.recovered", self._on_health_recovered)
                sub4 = self._event_bus.subscribe("budget.exhausted", self._on_budget_exhausted)
                self._subs = [sub1, sub2, sub3, sub4]
            except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(
                    "Could not subscribe to EventBus — degradation will use manual checks only"
                )

    async def stop(self) -> None:
        """Unsubscribe from EventBus events.

        v13.22.4: EventBus.subscribe() returns a subscription ID
        (string) per the canonical contract — restart.py and
        shutdown.py both treat the return value as a string and
        pass it to bus.unsubscribe(sub_id). The previous
        implementation here treated it as a Subscription object
        with an .unsubscribe() method, which raised AttributeError
        ('str' object has no attribute 'unsubscribe'); the
        contextlib.suppress(Exception) silently swallowed it,
        leaking subscriptions and causing the manager to keep
        firing after stop(). Fixed: use bus.unsubscribe(sub_id).
        """
        self._running = False
        bus = self._event_bus
        if bus is not None:
            for sub_id in self._subs:
                with contextlib.suppress(Exception):
                    bus.unsubscribe(sub_id)
        self._subs.clear()
        logger.info("DegradationManager stopped")

    async def _on_health_degraded(self, event: Any) -> None:
        # Single-system degradation — only step down 1 level
        await self._step_down(TriggerType.HEALTH_SCORE, "health degraded")

    async def _on_health_critical(self, event: Any) -> None:
        # Multiple critical systems — step down 2 levels
        await self._step_down(
            TriggerType.HEALTH_SCORE,
            "health critical",
            steps=2,
        )

    async def _on_health_recovered(self, event: Any) -> None:
        now = time.monotonic()
        if now - self._last_recovery_check < self._recovery_interval:
            return
        self._last_recovery_check = now
        await self._step_up(TriggerType.HEALTH_SCORE, "health recovered")

    async def _on_budget_exhausted(self, event: Any) -> None:
        await self._step_down(
            TriggerType.BUDGET,
            f"budget exhausted for {event.tenant_id}",
            to_level=DegradationLevel.EMERGENCY,
        )

    # ── Transitions ───────────────────────────────────────────────────────────

    async def _step_down(
        self,
        trigger: TriggerType,
        reason: str,
        steps: int = 1,
        to_level: DegradationLevel | None = None,
    ) -> None:
        async with self._lock:
            old_level = self._level
            if to_level is not None:
                new_level = to_level
            else:
                new_level = old_level
                for _ in range(steps):
                    new_level = _TRANSITION_DOWN[new_level]

            if new_level == old_level:
                return

            self._level = new_level
            await self._emit_change(old_level, new_level, trigger, reason)

    async def _step_up(
        self,
        trigger: TriggerType,
        reason: str,
        steps: int = 1,
        to_level: DegradationLevel | None = None,
    ) -> None:
        async with self._lock:
            old_level = self._level
            if to_level is not None:
                new_level = to_level
            else:
                new_level = old_level
                for _ in range(steps):
                    new_level = _TRANSITION_UP[new_level]

            if new_level == old_level:
                return

            self._level = new_level
            await self._emit_change(old_level, new_level, trigger, reason)

    async def _emit_change(
        self,
        old: DegradationLevel,
        new: DegradationLevel,
        trigger: TriggerType,
        reason: str,
    ) -> None:
        cfg = _LEVEL_CONFIG[new]
        state = DegradationState(
            level=new,
            previous_level=old,
            reason=reason,
            triggered_by=trigger,
            model_preference=cfg["model_preference"],
            enable_mcp=cfg["enable_mcp"],
            cache_only=cfg["cache_only"],
            queue_workflows=cfg["queue_workflows"],
            alert_operator=cfg["alert_operator"],
        )
        self._state_history.append(state.to_dict())
        logger.warning(
            f"Degradation changed {old.value} -> {new.value}"
            f" (reason: {reason}, trigger: {trigger.value})"
        )

        if self._event_bus is not None:
            try:
                event = __import__(
                    "beagle.events.events", fromlist=["DegradationChanged"]
                ).DegradationChanged(
                    workflow_id="system",
                    previous_level=old.value,
                    current_level=new.value,
                    reason=reason,
                )
                await self._event_bus.emit(event)
            except Exception:  # broad catch intentional
                logger.exception("Failed to emit DegradationChanged event")

    # ── Manual control ────────────────────────────────────────────────────────

    async def set_level(
        self,
        level: DegradationLevel,
        reason: str = "manual override",
    ) -> None:
        """Manually set degradation level.

        v13.22.4: take the same self._lock used by _step_up /
        _step_down. The previous version mutated self._level without
        the lock, racing with in-flight _step_down calls and
        silently losing set_level's update.
        """
        async with self._lock:
            if self._level == level:
                return
            old = self._level
            self._level = level
            await self._emit_change(old, level, TriggerType.MANUAL, reason)

    async def check_circuit_breakers(self) -> None:
        """Check circuit breaker health and potentially degrade."""
        try:
            from ..utils.circuit_breaker import get_circuit_health_report

            report = get_circuit_health_report()
            open_circuits = [name for name, data in report.items() if data.get("state") == "open"]
            critical_circuits = [
                name for name in open_circuits if "llm" in name.lower() or "model" in name.lower()
            ]

            if critical_circuits:
                await self._step_down(
                    TriggerType.CIRCUIT_BREAKER,
                    f"critical circuits open: {', '.join(critical_circuits[:3])}",
                )
            elif len(open_circuits) >= 3:
                await self._step_down(
                    TriggerType.CIRCUIT_BREAKER,
                    f"multiple circuits open ({len(open_circuits)})",
                )
        except Exception:  # broad catch intentional
            logger.exception("Circuit breaker health check failed")
