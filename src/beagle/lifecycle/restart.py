"""Restart trigger for Beagle graceful self-restart.

Monitors health events and triggers a coordinated checkpoint → drain →
shutdown → re-exec cycle when the system degrades past the critical
threshold for a configurable number of consecutive checks.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from .checkpoint import Checkpoint, get_checkpoint_manager
from .shutdown import get_shutdown_coordinator

logger = logging.getLogger("Beagle.lifecycle")


class RestartTrigger:
    """Monitors health events and triggers restart when critical.

    Subscribes to health.critical events. After `consecutive_critical_threshold`
    consecutive critical health checks, initiates graceful restart.
    """

    def __init__(
        self,
        consecutive_critical_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        max_restarts: int = 5,
    ) -> None:
        self._consecutive_critical_threshold = consecutive_critical_threshold
        self._cooldown_seconds = cooldown_seconds
        self._max_restarts = max_restarts
        self._consecutive_criticals = 0
        self._last_restart_time: float = 0.0
        self._restart_count = 0
        self._subscription_ids: list[str] = []
        self._armed = False
        # v13.22.4: lock around the counter+cooldown check-then-set
        # sequence. Without it, two concurrent _on_critical events
        # could each see >= threshold, each call _schedule_restart,
        # and spawn two parallel restart threads — each running
        # os.execv, which races the shutdown coordinator and produces
        # two concurrent processes holding the same checkpoint and
        # SQLite WAL. Audit §5 #9.
        self._restart_lock = threading.Lock()

    def arm(self) -> None:
        """Subscribe to health events and arm the restart trigger."""
        if self._armed:
            logger.warning("RestartTrigger already armed — skipping")
            return

        try:
            from beagle.events import get_event_bus

            bus = get_event_bus()

            # Subscribe to health.critical events
            critical_sub_id = bus.subscribe("health.critical", self._on_critical)
            # Subscribe to health.recovered events
            recovered_sub_id = bus.subscribe("health.recovered", self._on_recovered)

            self._subscription_ids = [critical_sub_id, recovered_sub_id]
            self._armed = True
            logger.info(
                "RestartTrigger armed: threshold=%d cooldown=%.0fs max_restarts=%d",
                self._consecutive_critical_threshold,
                self._cooldown_seconds,
                self._max_restarts,
            )
            try:
                _install_sighup_handler(self)
            except (OSError, ValueError, AttributeError):  # SIGHUP unavailable on some platforms
                logger.debug("SIGHUP handler not installed (platform unsupported)", exc_info=True)
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error("Failed to arm RestartTrigger: %s", exc)

    def disarm(self) -> None:
        """Unsubscribe from health events."""
        if not self._armed:
            return

        try:
            from beagle.events import get_event_bus

            bus = get_event_bus()
            for sub_id in self._subscription_ids:
                bus.unsubscribe(sub_id)
            self._subscription_ids = []
            self._armed = False
            logger.info("RestartTrigger disarmed")
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error("Failed to disarm RestartTrigger: %s", exc)

    def _on_critical(self, event: object) -> None:
        """Handle health.critical event — increment counter, check threshold."""
        # v13.22.4: take the lock for the entire check-then-act.
        # Counter increment + threshold check + (conditional) schedule
        # must be atomic w.r.t. concurrent _on_critical invocations.
        with self._restart_lock:
            self._consecutive_criticals += 1
            logger.warning(
                "Health critical received (consecutive=%d/%d)",
                self._consecutive_criticals,
                self._consecutive_critical_threshold,
            )
            if self._consecutive_criticals >= self._consecutive_critical_threshold:
                logger.critical(
                    "Consecutive critical threshold reached (%d) — triggering restart",
                    self._consecutive_criticals,
                )
                # Schedule the restart asynchronously — don't block the
                # event callback. The schedule itself is non-blocking
                # (it just starts a daemon thread); the lock guard above
                # ensures only one threshold-triggered restart is
                # scheduled per critical-streak.
                self._schedule_restart_locked("health_critical")

    def _on_recovered(self, event: object) -> None:
        """Handle health.recovered event — reset counter."""
        with self._restart_lock:
            self._consecutive_criticals = 0
        logger.info("Health recovered — critical counter reset to 0")

    def _schedule_restart_locked(self, reason: str) -> None:
        """Schedule a restart while holding _restart_lock.

        Acquires the lock, then spawns the daemon thread which does
        the actual work. The spawn itself is cheap; the lock guards
        against two concurrent spawns during the same critical
        streak. v13.22.4.
        """
        thread = threading.Thread(
            target=self._run_restart_sync,
            args=(reason,),
            daemon=True,
            name="beagle-restart",
        )
        thread.start()

    def _run_restart_sync(self, reason: str) -> None:
        """Run the async restart in a new event loop."""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.trigger_restart(reason))
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.critical("Restart failed: %s", exc)
        finally:
            loop.close()

    async def trigger_restart(self, reason: str = "health_critical") -> None:
        """Execute graceful restart sequence:

        1. Check cooldown (skip if too recent)
        2. Check max_restarts (give up if exceeded)
        3. Collect checkpoint from all subsystems
        4. Save checkpoint via CheckpointManager
        5. Shutdown via ShutdownCoordinator
        6. Re-exec process via os.execv()
        """
        now = time.time()

        # v13.22.4: cooldown + max_restarts check is racy without
        # the lock; two concurrent restart threads could each pass
        # the cooldown check before either sets _last_restart_time,
        # producing a double os.execv. Take the lock for the
        # check-then-set sequence.
        with self._restart_lock:
            # 1. Check cooldown
            if now - self._last_restart_time < self._cooldown_seconds:
                remaining = self._cooldown_seconds - (now - self._last_restart_time)
                logger.warning(
                    "Restart cooldown active (%.0fs remaining) — skipping",
                    remaining,
                )
                return

            # 2. Check max_restarts
            if self._restart_count >= self._max_restarts:
                logger.critical(
                    "Max restarts reached (%d/%d) — giving up, staying running",
                    self._restart_count,
                    self._max_restarts,
                )
                return

            self._restart_count += 1
            self._last_restart_time = now

        # 3. Collect checkpoint
        checkpoint = self._collect_checkpoint(reason)

        # 4. Save checkpoint
        try:
            mgr = get_checkpoint_manager()
            mgr.save(checkpoint)
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error("Failed to save checkpoint: %s", exc)

        # 5. Publish RestartTriggered event (after checkpoint attempt)
        self._publish_restart_triggered(reason)

        # 5. Shutdown
        try:
            coordinator = get_shutdown_coordinator()
            await coordinator.shutdown(reason=reason)
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error("Shutdown failed: %s", exc)

        # 6. Re-exec
        logger.critical(
            "Restarting Beagle: reason=%s restart_count=%d",
            reason,
            self._restart_count,
        )
        _re_exec()

    async def manual_restart(self, reason: str = "manual") -> None:
        """Manually triggered restart (e.g., from CLI or SIGHUP)."""
        logger.info("Manual restart requested: reason=%s", reason)
        await self.trigger_restart(reason)

    def _collect_checkpoint(self, reason: str) -> Checkpoint:
        """Collect state from all subsystems for checkpoint."""
        from beagle import __version__

        checkpoint = Checkpoint(
            timestamp=time.time(),
            version=__version__,
            restart_reason=reason,
            restart_count=self._restart_count,
            pid_before_restart=os.getpid(),
        )

        # Health monitor state
        try:
            from beagle.health import get_health_monitor

            monitor = get_health_monitor()
            checkpoint.health_previous_state = monitor._previous_state
            checkpoint.health_previous_score = monitor._previous_score
        except Exception:  # broad catch intentional
            logger.debug("Failed to collect health state", exc_info=True)

        # Circuit breaker states (async — skip in sync context, best effort)
        try:
            # Circuit breakers are async; we collect what we can synchronously
            # The _circuits dict is module-level, but access is async-locked
            # We do a best-effort read
            import beagle.utils.circuit_breaker as cb_mod

            # _circuits is protected by asyncio.Lock, so we read synchronously
            # for best-effort collection during shutdown
            for name, breaker in list(cb_mod._circuits.items()):
                checkpoint.circuit_states[name] = breaker.state.value
        except Exception:  # broad catch intentional
            logger.debug("Failed to collect circuit breaker states", exc_info=True)

        return checkpoint

    def _publish_restart_triggered(self, reason: str) -> None:
        """Publish RestartTriggered event."""
        try:
            from beagle.events import get_event_bus
            from beagle.events.events import RestartTriggered

            checkpoint_saved = True  # Will be saved after this
            event = RestartTriggered(
                workflow_id="lifecycle",
                reason=reason,
                restart_count=self._restart_count,
                checkpoint_saved=checkpoint_saved,
            )
            get_event_bus().publish(event)
        except Exception:  # broad catch intentional
            logger.debug("Failed to publish RestartTriggered event", exc_info=True)

    @property
    def consecutive_criticals(self) -> int:
        """Current count of consecutive critical health events."""
        return self._consecutive_criticals

    @property
    def restart_count(self) -> int:
        """Total number of restarts triggered."""
        return self._restart_count

    @property
    def is_armed(self) -> bool:
        """Whether the trigger is currently armed."""
        return self._armed


def _re_exec() -> None:
    """Replace current process with fresh instance."""
    # nosec B606 - execv without a shell is the point: the process re-executes
    # itself using sys.executable and its own argv, neither of which passes
    # through a shell.
    os.execv(sys.executable, [sys.executable, *sys.argv])  # nosec B606


def _install_sighup_handler(trigger: RestartTrigger) -> None:
    """Register SIGHUP handler that calls trigger_restart("signal").

    Only register in main thread — signal handlers can only be
    installed from the main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        logger.warning("Not in main thread — skipping SIGHUP handler")
        return

    def _sighup_handler(signum: int, _frame: object) -> None:
        logger.info("SIGHUP received — triggering restart")
        trigger._schedule_restart_locked("signal")

    try:
        signal.signal(signal.SIGHUP, _sighup_handler)
        logger.info("SIGHUP handler installed for graceful restart")
    except (OSError, ValueError) as exc:
        logger.warning("Failed to install SIGHUP handler: %s", exc)


# ── Module-level convenience ────────────────────────────────────────────────────


async def graceful_restart(reason: str = "manual") -> None:
    """Convenience function: create a RestartTrigger and trigger restart."""
    trigger = RestartTrigger()
    await trigger.trigger_restart(reason)
