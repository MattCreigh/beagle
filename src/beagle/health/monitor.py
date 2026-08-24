"""Periodic self-health monitoring for Beagle dark factory operation.

Runs a background loop that collects :class:`HealthSnapshot` instances
at configurable intervals, publishes events via the :class:`EventBus`,
and logs warnings/errors when thresholds are crossed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import TYPE_CHECKING

from .collector import HealthSnapshot, collect_snapshot
from .thresholds import HealthThresholds

if TYPE_CHECKING:
    pass  # reserved for future type-only imports

logger = logging.getLogger("Beagle.health")


class HealthMonitor:
    """Periodic self-health monitoring for Beagle dark factory operation.

    Collects health snapshots at configurable intervals, publishes events
    on state transitions (normal → degraded → critical → recovered), and
    maintains a rolling history for trend analysis.
    """

    def __init__(self, thresholds: HealthThresholds | None = None) -> None:
        self._thresholds = thresholds or HealthThresholds()
        self._history: deque[HealthSnapshot] = deque(maxlen=60)
        self._running = False
        self._task: asyncio.Task | None = None
        self._previous_state: str = "normal"  # "normal", "degraded", "critical"
        self._previous_score: float = 1.0

    async def start(self) -> None:
        """Start the periodic health check loop."""
        if self._running:
            logger.warning("Health monitor already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Health monitor started (interval=%ds)",
            self._thresholds.check_interval_seconds,
        )

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        logger.info("Health monitor stopped")

    async def _run_loop(self) -> None:
        """Main monitoring loop — collects snapshots at regular intervals."""
        while self._running:
            try:
                await self.check_now()
            except asyncio.CancelledError:
                raise
            except Exception:  # broad catch intentional
                logger.debug("Health check failed", exc_info=True)

            await asyncio.sleep(self._thresholds.check_interval_seconds)

    async def check_now(self) -> HealthSnapshot:
        """Run a single health check immediately.

        Collects metrics from all subsystems, calculates the composite
        health score, emits events on state transitions, and logs
        warnings/errors for degraded or critical states.
        """
        snapshot = collect_snapshot(self._thresholds)
        self._history.append(snapshot)

        # Always emit HealthCheckCompleted
        self._emit_check_completed(snapshot)

        # Determine new state and emit transition events
        new_state = self._compute_state(snapshot.health_score)
        self._handle_state_transition(new_state, snapshot)

        # Log based on state
        if new_state == "critical":
            logger.error(
                "Health CRITICAL: score=%.2f, systems=%s",
                snapshot.health_score,
                snapshot.critical_systems,
            )
        elif new_state == "degraded":
            logger.warning(
                "Health DEGRADED: score=%.2f, systems=%s",
                snapshot.health_score,
                snapshot.degraded_systems,
            )
        else:
            logger.debug(
                "Health check: score=%.2f rss=%.1fMB fd=%d circuits=%d",
                snapshot.health_score,
                snapshot.rss_mb,
                snapshot.fd_count,
                snapshot.circuits_open,
            )

        self._previous_state = new_state
        self._previous_score = snapshot.health_score
        return snapshot

    def _compute_state(self, score: float) -> str:
        """Determine health state from score using thresholds."""
        if score < self._thresholds.critical_score:
            return "critical"
        elif score < self._thresholds.degraded_score:
            return "degraded"
        return "normal"

    def _handle_state_transition(self, new_state: str, snapshot: HealthSnapshot) -> None:
        """Emit events on state transitions — never on steady state."""
        old_state = self._previous_state

        # No transition → no event (avoid spamming)
        if old_state == new_state:
            return

        # Transitions
        if new_state == "degraded" and old_state == "normal":
            self._emit_degraded(snapshot)
        elif new_state == "critical" and old_state in ("normal", "degraded"):
            self._emit_critical(snapshot)
        elif new_state == "normal" and old_state in ("degraded", "critical"):
            self._emit_recovered(snapshot)

    def _emit_check_completed(self, snapshot: HealthSnapshot) -> None:
        """Publish HealthCheckCompleted event."""
        try:
            from beagle.events import get_event_bus
            from beagle.events.events import HealthCheckCompleted

            event = HealthCheckCompleted(
                workflow_id="health-monitor",
                health_score=snapshot.health_score,
                rss_mb=snapshot.rss_mb,
                fd_count=snapshot.fd_count,
                circuits_open=snapshot.circuits_open,
                pool_active=snapshot.pool_active,
                degraded_systems=tuple(snapshot.degraded_systems),
                critical_systems=tuple(snapshot.critical_systems),
            )
            get_event_bus().publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # broad catch intentional
            logger.debug("Failed to emit HealthCheckCompleted event", exc_info=True)

    def _emit_degraded(self, snapshot: HealthSnapshot) -> None:
        """Publish HealthDegraded event."""
        try:
            from beagle.events import get_event_bus
            from beagle.events.events import HealthDegraded

            event = HealthDegraded(
                workflow_id="health-monitor",
                health_score=snapshot.health_score,
                degraded_systems=tuple(snapshot.degraded_systems),
                critical_systems=tuple(snapshot.critical_systems),
            )
            get_event_bus().publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # broad catch intentional
            logger.debug("Failed to emit HealthDegraded event", exc_info=True)

    def _emit_critical(self, snapshot: HealthSnapshot) -> None:
        """Publish HealthCritical event."""
        try:
            from beagle.events import get_event_bus
            from beagle.events.events import HealthCritical

            event = HealthCritical(
                workflow_id="health-monitor",
                health_score=snapshot.health_score,
                critical_systems=tuple(snapshot.critical_systems),
            )
            get_event_bus().publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # broad catch intentional
            logger.debug("Failed to emit HealthCritical event", exc_info=True)

    def _emit_recovered(self, snapshot: HealthSnapshot) -> None:
        """Publish HealthRecovered event."""
        try:
            from beagle.events import get_event_bus
            from beagle.events.events import HealthRecovered

            event = HealthRecovered(
                workflow_id="health-monitor",
                health_score=snapshot.health_score,
                previous_score=self._previous_score,
            )
            get_event_bus().publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # broad catch intentional
            logger.debug("Failed to emit HealthRecovered event", exc_info=True)

    @property
    def latest(self) -> HealthSnapshot | None:
        """Most recent snapshot, or None if no checks have run."""
        if self._history:
            return self._history[-1]
        return None

    @property
    def history(self) -> list[HealthSnapshot]:
        """Recent snapshot history (up to 60 entries)."""
        return list(self._history)

    def trend(self, metric: str, window: int = 5) -> str:
        """Return 'improving', 'stable', or 'degrading' for a metric.

        Analyzes the last ``window`` snapshots and determines if the given
        metric is trending up, stable, or down.

        Args:
            metric: Name of a numeric HealthSnapshot field (e.g. 'health_score',
                    'rss_mb', 'fd_count').
            window: Number of recent snapshots to analyze.

        Returns:
            'improving' if the metric is trending better,
            'degrading' if trending worse,
            'stable' if no clear trend.

        """
        if len(self._history) < 2:
            return "stable"

        snapshots = list(self._history)[-window:]

        # For metrics where lower is better
        lower_is_better = {
            "rss_mb",
            "fd_count",
            "circuits_open",
            "pool_failed",
            "zombie_child_count",
            "thread_count",
            "rate_limiter_blocked",
            "rate_limiter_utilization",
        }

        values: list[float] = []
        for snap in snapshots:
            val = getattr(snap, metric, None)
            if val is not None and isinstance(val, int | float):
                values.append(float(val))

        if len(values) < 2:
            return "stable"

        # Simple trend: compare first half average vs second half average
        mid = len(values) // 2
        first_half = sum(values[:mid]) / max(1, mid)
        second_half = sum(values[mid:]) / max(1, len(values) - mid)

        # Avoid division by zero
        if abs(first_half) < 1e-10:
            return "stable"

        change_pct = (second_half - first_half) / abs(first_half)

        # Threshold for meaningful change
        significant = 0.05  # 5% change

        if abs(change_pct) < significant:
            return "stable"

        if metric in lower_is_better or metric == "health_score":
            # For health_score, up is improving
            if metric == "health_score":
                return "improving" if change_pct > 0 else "degrading"
            # For lower-is-better metrics, down is improving
            return "improving" if change_pct < 0 else "degrading"
        else:
            # For other metrics, up is improving
            return "improving" if change_pct > 0 else "degrading"


# ── Module-level singleton ──────────────────────────────────────────────────────

_monitor: HealthMonitor | None = None
_monitor_lock = threading.Lock()


def get_health_monitor() -> HealthMonitor:
    """Get the global health monitor singleton."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = HealthMonitor()
    return _monitor


async def run_health_check() -> HealthSnapshot:
    """Convenience: run a one-shot health check."""
    return await get_health_monitor().check_now()
