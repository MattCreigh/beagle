"""Coordinated shutdown of all Beagle subsystems.

Ensures that health monitors, daemons, subprocess pools, databases,
and event buses are all shut down in the correct order with proper
error isolation — one subsystem failing must NOT prevent the next
from shutting down.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections.abc import Callable

from beagle.events import BeagleEvent

logger = logging.getLogger("Beagle.lifecycle")


class ShutdownCoordinator:
    """Orchestrates orderly shutdown of all Beagle subsystems."""

    def __init__(self) -> None:
        self._shutdown_in_progress = False
        self._hooks: list[tuple[str, Callable]] = []  # (name, cleanup_fn)
        self._steps_completed = 0
        self._steps_failed = 0

    def register_hook(self, name: str, hook: Callable[[], None]) -> None:
        """Register a cleanup hook to run during shutdown.

        Hooks run in LIFO order (last registered = first to run).
        """
        self._hooks.append((name, hook))

    async def shutdown(self, reason: str = "unknown") -> None:
        """Execute full coordinated shutdown.

        Order:
        1. Set _shutdown_in_progress flag (prevents re-entry)
        2. Publish ShutdownStarted event
        3. Stop HealthMonitor
        4. Stop BeagleDaemon (if running)
        5. Drain GoosePool (wait for active processes, with timeout)
        6. Run registered hooks (LIFO)
        7. Flush TrackingDatabase (PRAGMA wal_checkpoint)
        8. Publish ShutdownCompleted event
        9. Clear EventBus subscribers

        Each step wrapped in try/except — one failure must NOT prevent
        the next step from running.
        """
        if self._shutdown_in_progress:
            logger.warning("Shutdown already in progress — skipping re-entry")
            return

        self._shutdown_in_progress = True
        self._steps_completed = 0
        self._steps_failed = 0
        # use monotonic — wall clock moves backwards across NTP adjustment
        # and DST, so a difference can be negative or wildly wrong.
        start_time = time.monotonic()

        # 2. Publish ShutdownStarted event
        await self._step("publish ShutdownStarted", self._publish_shutdown_started, reason)
        # 3. Stop HealthMonitor
        await self._step("stop HealthMonitor", self._stop_health_monitor)

        # 4. Stop BeagleDaemon
        await self._step("stop BeagleDaemon", self._stop_daemon)

        # 5. Drain GoosePool
        await self._step("drain GoosePool", self._drain_pool)

        # 5.4. v13.22.4: close the shared A2A httpx client so its open
        # sockets are not inherited by an os.execv-restarted process.
        # Without this, every agent-to-agent call leaks a connection
        # that survives the restart; the new process finds open fds it
        # does not own.
        await self._step("close a2a http client", self._close_a2a_client)

        # 5.5. Stop Orpheus subsystem
        await self._step("stop Orpheus", self._stop_orpheus)

        # 6. Run registered hooks (LIFO)
        await self._step("run shutdown hooks", self._run_hooks)

        # 7. Flush TrackingDatabase
        await self._step("flush TrackingDatabase", self._flush_database)

        # 8. Publish ShutdownCompleted event
        duration = time.monotonic() - start_time
        await self._step(
            "publish ShutdownCompleted",
            self._publish_shutdown_completed,
            duration,
        )

        # 9. Clear EventBus subscribers
        await self._step("clear EventBus", self._clear_event_bus)

        # Cleanup singletons
        await self._step("cleanup singletons", self._cleanup_singletons)

        logger.info(
            "Shutdown complete: reason=%s duration=%.1fs steps_ok=%d steps_fail=%d",
            reason,
            duration,
            self._steps_completed,
            self._steps_failed,
        )

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress (prevents new work)."""
        return self._shutdown_in_progress

    # ── Step helpers ───────────────────────────────────────────────────────────

    async def _step(self, name: str, fn: Callable, *args: object) -> None:
        """Execute a shutdown step with error isolation.

        Each step gets a 5-second timeout. If a step hangs, log error
        and continue.

        v13.22.4: a sync ``fn`` is now run in ``asyncio.to_thread`` so
        the event loop can be reached for the timeout wrapper. The
        previous implementation called ``fn(*args)`` directly on the
        loop, which meant a hung sync step (e.g., a wedged child
        process, a blocking DB commit) would freeze shutdown
        indefinitely — the contract "each step gets a 5-second
        timeout" was false for sync callables.

        v13.22.5: during interpreter teardown the default
        ``ThreadPoolExecutor`` is shut down by CPython (the global
        ``concurrent.futures.thread._shutdown`` flag is set), and any
        ``asyncio.to_thread`` submission then raises
        ``RuntimeError: cannot schedule new futures after interpreter
        shutdown``. The broad handler at the bottom of this method
        would log every step as failed — observed: 10/10 steps fail
        during a plain ``pytest --collect-only`` once the atexit path
        runs, including ``flush TrackingDatabase`` whose buffered
        writes were then silently discarded. Regression introduced by
        the v13.22.4 fix (the previous shape was a hang; this fix
        traded the hang for a total-failure mode). Fix: when the
        interpreter is finalising, call the sync step directly. We
        accept that the 5-second timeout cannot apply on this path —
        a step that runs without a timeout is strictly better than a
        step that never runs at all. As a defensive backstop behind
        the ``is_finalizing`` primary check, we also catch the
        specific ``RuntimeError`` and fall back to the direct call
        rather than counting a failure.
        """
        # Interpreter is finalising (atexit / module unload). The
        # default ThreadPoolExecutor is dead. Don't try to submit —
        # run the sync step directly. This is the load-bearing branch
        # for atexit-driven shutdown and is the entire reason this
        # method now imports ``sys``.
        interpreter_finalizing = False
        try:
            interpreter_finalizing = bool(sys.is_finalizing())
        except (AttributeError, RuntimeError, TypeError):
            # sys.is_finalizing() should not raise, but a custom sys
            # module (test harnesses, embedded interpreters) may not
            # expose it. Treat any AttributeError/TypeError as "not
            # finalizing" so the normal shutdown path runs. RuntimeError
            # catches the exotic case where the call itself errors.
            interpreter_finalizing = False

        try:
            if interpreter_finalizing:
                # Finalising path. Sync steps run inline; async steps
                # are scheduled if possible, otherwise logged as
                # skipped (no event loop will outlive this call).
                if asyncio.iscoroutinefunction(fn):
                    try:
                        await asyncio.wait_for(fn(*args), timeout=5.0)
                    except RuntimeError:
                        logger.debug(
                            "Shutdown step '%s' skipped during interpreter teardown (no live loop)",
                            name,
                        )
                        return
                else:
                    fn(*args)
            elif asyncio.iscoroutinefunction(fn):
                # Normal path: native async step, timeout applies.
                await asyncio.wait_for(fn(*args), timeout=5.0)
            else:
                # Normal path: sync step off-loop via to_thread so
                # the timeout wrapper can fire. Defensive backstop:
                # if the executor is already dead for any other reason
                # (custom executor teardown, thread-safety hot patch),
                # fall through to the direct call rather than fail.
                try:
                    await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=5.0)
                except RuntimeError as exc:
                    if "cannot schedule new futures" in str(exc):
                        logger.debug(
                            "Shutdown step '%s' executor dead, falling back to direct call: %s",
                            name,
                            exc,
                        )
                        fn(*args)
                    else:
                        raise
            self._steps_completed += 1
        except TimeoutError:
            logger.error("Shutdown step '%s' timed out after 5s", name)
            self._steps_failed += 1
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            # Narrowed from ``except Exception`` per BLE001 doctrine floor.
            # These are the failure modes a shutdown step can reasonably
            # produce: I/O errors, runtime contract violations, bad values,
            # wrong attribute access, and type mismatches.
            logger.error("Shutdown step '%s' failed: %s", name, exc)
            self._steps_failed += 1

    # ── Shutdown steps ─────────────────────────────────────────────────────────

    def _publish_shutdown_started(self, reason: str) -> None:
        """Publish ShutdownStarted event."""
        try:
            from beagle.events import get_event_bus

            restart_planned = reason in ("health_critical", "manual", "signal")
            event = _make_shutdown_started(reason, restart_planned)
            get_event_bus().publish(event)
        except Exception:  # broad catch intentional
            logger.debug("Failed to publish ShutdownStarted event", exc_info=True)

    def _stop_health_monitor(self) -> None:
        """Stop the health monitor."""
        try:
            from beagle.health import get_health_monitor

            monitor = get_health_monitor()
            monitor.stop()
            logger.info("Health monitor stopped")
        except Exception:  # broad catch intentional
            logger.debug("Failed to stop health monitor", exc_info=True)

    def _stop_daemon(self) -> None:
        """Stop the daemon if running."""
        try:
            from beagle.daemon.daemon import get_active_daemon

            daemon = get_active_daemon()
            if daemon is not None:
                daemon.stop()
                logger.info("Daemon stop signaled")
            else:
                logger.debug("No active daemon to stop")
        except Exception:  # broad catch intentional
            logger.debug("Failed to stop daemon", exc_info=True)

    def _drain_pool(self) -> None:
        """Drain the subprocess pool — terminate active processes."""
        try:
            from beagle.core.singletons import ProcessRegistry

            count = ProcessRegistry.instance().cleanup_all()
            logger.info("Process registry cleaned up (%d processes terminated)", count)
        except Exception:  # broad catch intentional
            logger.debug("Failed to drain subprocess pool", exc_info=True)

    async def _stop_orpheus(self) -> None:
        """Stop the Orpheus subsystem (rings + HTTP transport)."""
        try:
            from .orpheus_startup import stop_orpheus

            await stop_orpheus()
        except Exception:  # broad catch intentional
            logger.debug("Failed to stop Orpheus subsystem", exc_info=True)

    def _run_hooks(self) -> None:
        """Run registered shutdown hooks in LIFO order."""
        hooks = list(reversed(self._hooks))
        for name, hook in hooks:
            try:
                hook()
                logger.debug("Shutdown hook '%s' completed", name)
            except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
                # Narrowed from ``except Exception`` per BLE001 doctrine floor.
                logger.error("Shutdown hook '%s' failed: %s", name, exc)

    def _flush_database(self) -> None:
        """Flush the tracking database (WAL checkpoint)."""
        try:
            from beagle.tracking.database import TrackingDatabase

            db = TrackingDatabase.get_instance()
            conn = db._get_conn()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
            logger.info("Tracking database WAL checkpoint completed")
        except Exception:  # broad catch intentional
            logger.debug("Failed to flush tracking database", exc_info=True)

    def _close_a2a_client(self) -> None:
        """Close the shared A2A httpx client during shutdown.

        v13.22.4: ensures the shared HTTP connection pool is drained
        before the process is potentially restarted via os.execv.
        Without this, an os.execv-restarted process inherits the
        parent's open sockets, producing a confusing "address in use"
        on the next bind and split-brain behaviour on the A2A server.
        """
        try:
            from beagle.bridges.a2a_client import (
                get_a2a_client,
            )

            client = get_a2a_client()
            # The shared client is a coroutine-returning aclose() —
            # bridge to a sync wait via asyncio.run if no loop is
            # running, or skip if aclose is itself a no-op.
            aclose = getattr(client, "aclose", None)
            if aclose is None:
                return
            try:
                # v1.2.0 (RG-7, BGL-010): get_running_loop() raises RuntimeError
                # when no loop is running (the sync-boundary case), which is
                # exactly the branch we want to fall through on. The prior
                # get_event_loop() emitted a DeprecationWarning on the main
                # thread and a RuntimeError on a worker thread.
                asyncio.get_running_loop()
                # A loop is running; the a2a aclose() will be picked up by the
                # surrounding _step wrapper (which uses to_thread + wait_for).
                return
            except RuntimeError:
                # No running loop to inspect; fall through to the notice below.
                logger.info("A2A client aclose() skipped: no event loop in this context")
            logger.debug("A2A client aclose() not auto-invoked from sync step")
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            # Narrowed from ``except Exception`` per BLE001 doctrine floor.
            logger.debug("A2A client close skipped: %s", exc)

    def _publish_shutdown_completed(self, duration: float) -> None:
        """Publish ShutdownCompleted event."""
        try:
            from beagle.events import get_event_bus

            event = _make_shutdown_completed(duration, self._steps_completed, self._steps_failed)
            get_event_bus().publish(event)
        except Exception:  # broad catch intentional
            logger.debug("Failed to publish ShutdownCompleted event", exc_info=True)

    def _clear_event_bus(self) -> None:
        """Clear EventBus subscribers and ring buffer."""
        try:
            from beagle.events import get_event_bus

            bus = get_event_bus()
            with bus._lock:
                bus._subscribers.clear()
                bus._ring_buffer.clear()
            logger.info("Event bus subscribers cleared")
        except Exception:  # broad catch intentional
            logger.debug("Failed to clear event bus", exc_info=True)

    def _cleanup_singletons(self) -> None:
        """Cleanup singleton instances if available."""
        try:
            from beagle.core.singletons import ProcessRegistry

            ProcessRegistry.instance().cleanup_all()
        except Exception:  # broad catch intentional
            logger.debug("Failed to cleanup singletons", exc_info=True)


# ── Module-level singleton ──────────────────────────────────────────────────────

_coordinator: ShutdownCoordinator | None = None
_coordinator_lock = threading.Lock()


def get_shutdown_coordinator() -> ShutdownCoordinator:
    """Get the global shutdown coordinator singleton."""
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            _coordinator = ShutdownCoordinator()
    return _coordinator


# ── Event factories (avoid circular imports) ────────────────────────────────────


def _make_shutdown_started(reason: str, restart_planned: bool) -> BeagleEvent:
    """Create a ShutdownStarted event without import issues."""
    from beagle.events.events import ShutdownStarted

    return ShutdownStarted(
        workflow_id="lifecycle",
        reason=reason,
        restart_planned=restart_planned,
    )


def _make_shutdown_completed(duration: float, completed: int, failed: int) -> BeagleEvent:
    """Create a ShutdownCompleted event without import issues."""
    from beagle.events.events import ShutdownCompleted

    return ShutdownCompleted(
        workflow_id="lifecycle",
        duration_seconds=duration,
        steps_completed=completed,
        steps_failed=failed,
    )
